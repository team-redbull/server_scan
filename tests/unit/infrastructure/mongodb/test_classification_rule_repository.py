"""Unit test (no I/O) for `default_system_rules` — pure object
construction, no Mongo connection involved.

The important assertions here are behavioural, not structural: the seeded
patterns are checked against the estate's *real* hostnames, resolved
through the actual `classify()` engine rather than a naive "which
patterns match" check. That distinction matters since 2026-09-08: every
default is now a broad prefix/substring catch-all (`^ocp4-hypershift`,
`^ocp-`, `mce`, `^ocp4`) rather than a narrow, mutually-exclusive shape,
so several of them textually match the same hostname — only
`classify()`'s real priority/order resolution, not raw pattern matching,
says which one actually wins. A test that only asserted "there are three
rules with these names" would have happily passed the previous
`^ocp-.*` / `^upi-.*` defaults, which matched none of the names this
platform actually ingests.
"""

from __future__ import annotations

import re

import pytest

from app.domain.enums import InstallationType, Vendor
from app.domain.models.classification_rule import PRIORITY_BANDS
from app.domain.services.classification import ClassifiableServer, classify
from app.domain.services.regex_engine import RegexModuleEngine
from app.domain.value_objects.site import site_catalog
from app.infrastructure.mongodb.classification_rule_repository import (
    _UPI_PATTERN,
    default_system_rules,
)

# The shipped default catalog. The seeded rules interpolate the
# configured site codes now, so these fixtures pin which set they
# were written against.
SITES = site_catalog("")

ENGINE = RegexModuleEngine(max_pattern_length=200, match_timeout_seconds=0.25)


def _classify(name: str) -> InstallationType:
    """The installation type the real resolution engine gives `name`
    against the current shipped default rules.
    """
    server = ClassifiableServer(name=name, vendor=Vendor.DELL, manager_type=None, site_id=None)
    return classify(server, default_system_rules(SITES), ENGINE).installation_type


def test_rules_have_unique_ids_and_names() -> None:
    rules = default_system_rules(SITES)
    assert len({r.id for r in rules}) == len(rules)
    assert len({r.name for r in rules}) == len(rules)


def test_every_default_rule_is_a_locked_unscoped_system_rule() -> None:
    for rule in default_system_rules(SITES):
        assert rule.source == "SYSTEM_DEFAULT"
        assert rule.system is True
        assert rule.enabled is True
        assert rule.field == "name"
        low, high = PRIORITY_BANDS["SYSTEM_DEFAULT"]
        assert low <= rule.priority <= high
        # Unscoped: these encode a fleet-wide naming convention, not a
        # per-vendor or per-site preference.
        assert rule.scope.vendor is None
        assert rule.scope.manager_type is None
        assert rule.scope.site_id is None


@pytest.mark.parametrize(
    "name",
    [
        "ocp4-hypershift-five-01",
        "ocp4-hypershift-data-five-02",
        "ocp4-hypershift-bat-yam-99",
        "ocp-dell-r660-five-128c-1024gb-FCH1234567",
        "ocp-cisco-m6-nyc-64c-512gb-CIS0000124",
        # Broadened 2026-09-08: both hosted-cluster rules are now bare
        # prefix matches, so neither the site token nor the rest of the
        # old structured shape is required any more.
        "ocp4-hypershift",
        "ocp4-hypershift-anything-at-all",
        "ocp-anything-goes-here",
    ],
)
def test_hosted_cluster_hostnames_classify_as_hosted_cluster(name: str) -> None:
    """Every one of these also textually matches UPI's `ocp4` catch-all
    (the hypershift ones do; the ocp-<vendor>-<model>-... ones don't, since
    that family never carries the `ocp4` prefix at all) — HOSTED_CLUSTER's
    rules sort ahead of UPI's, so they win regardless.
    """
    assert _classify(name) == InstallationType.HOSTED_CLUSTER


@pytest.mark.parametrize(
    "name",
    [
        "ocp4-mce-five-01",
        "ocp4-mce-nyc-07",
        "ocp4-prod-mce-tlv-03",
        "OCP4-MCE-FIVE-01",  # ignore_case is the shared default for every rule
    ],
)
def test_mce_hostnames_classify_as_mce(name: str) -> None:
    """Every one of these also textually matches UPI's `ocp4` catch-all —
    MCE's rule sorts ahead of it (order=2 vs. UPI's order=3), so an MCE
    hub's own nodes are pulled out of the generic UPI bucket.
    """
    assert _classify(name) == InstallationType.MCE


@pytest.mark.parametrize(
    "name",
    [
        "ocp4-five-compute-01",
        "ocp4-nyc-control-plane-02",
        "ocp4-prod-tlv-infra-01",
        "ocp4-prep-five-compute-01",
        "ocp4-bat-yam-infra-07",
        # Broadened 2026-09-08: UPI is now a bare `ocp4` prefix catch-all,
        # so a name whose site token is invalid, or missing entirely, is
        # no longer UNCLASSIFIED — only HOSTED_CLUSTER's and MCE's more
        # specific patterns are still checked for structure at all.
        "ocp4-tlvx-01",  # "tlv" is a substring, not a real site token
        "ocp4-prod-infra-01",  # no site token
    ],
)
def test_upi_hostnames_classify_as_upi(name: str) -> None:
    assert _classify(name) == InstallationType.UPI


@pytest.mark.parametrize(
    "name",
    [
        "random-server-0009",
        "some-unmanaged-box",
        "",
    ],
)
def test_unrecognized_hostnames_match_nothing(name: str) -> None:
    assert _classify(name) == InstallationType.UNCLASSIFIED


def test_hosted_cluster_and_mce_outrank_the_upi_catch_all() -> None:
    """UPI's pattern is a broad `ocp4` prefix that textually matches every
    HOSTED_CLUSTER and MCE hostname too — this is deliberate (2026-09-08),
    not a regression of the mutual-exclusivity the previous design had.
    `order` is what keeps the more specific rules winning: HOSTED_CLUSTER
    (order 0-1) and MCE (order 2) both sort ahead of UPI (order 3), so
    `classify()`'s first-match-wins never reaches UPI for these names even
    though UPI's own pattern matches every one of them too.
    """
    names_and_expected = [
        ("ocp4-hypershift-five-01", InstallationType.HOSTED_CLUSTER),
        ("ocp4-hypershift-data-five-02", InstallationType.HOSTED_CLUSTER),
        ("ocp4-mce-five-01", InstallationType.MCE),
    ]
    for name, expected in names_and_expected:
        assert re.search(_UPI_PATTERN, name), f"{name} was expected to also match UPI's pattern"
        assert _classify(name) == expected
