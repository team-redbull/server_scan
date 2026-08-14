"""Unit test (no I/O) for `default_system_rules` — pure object
construction, no Mongo connection involved.

The important assertions here are behavioural, not structural: the seeded
patterns are checked against the estate's *real* hostnames. A test that
only asserted "there are three rules with these names" would have happily
passed the previous `^ocp-.*` / `^upi-.*` defaults, which matched none of
the names this platform actually ingests.
"""

from __future__ import annotations

import re

import pytest

from app.domain.enums import InstallationType
from app.domain.models.classification_rule import PRIORITY_BANDS
from app.infrastructure.mongodb.classification_rule_repository import default_system_rules


def _classify(name: str) -> set[InstallationType]:
    """Every installation type whose rule matches `name`. A set, so the
    tests can assert that exactly one rule claims each hostname.
    """
    return {
        rule.installation_type
        for rule in default_system_rules()
        if re.compile(rule.pattern, re.IGNORECASE).match(name)
    }


def test_rules_have_unique_ids_and_names() -> None:
    rules = default_system_rules()
    assert len({r.id for r in rules}) == len(rules)
    assert len({r.name for r in rules}) == len(rules)


def test_every_default_rule_is_a_locked_unscoped_system_rule() -> None:
    for rule in default_system_rules():
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
        "ocp4-hypershift-one-99",
        "ocp-dell-r660-five-128c-1024gb-FCH1234567",
        "ocp-cisco-m6-two-64c-512gb-CIS0000124",
    ],
)
def test_hosted_cluster_hostnames_classify_as_hosted_cluster(name: str) -> None:
    assert _classify(name) == {InstallationType.HOSTED_CLUSTER}


@pytest.mark.parametrize(
    "name",
    [
        "ocp4-five-compute-01",
        "ocp4-one-control-plane-02",
        "ocp4-prod-one-infra-01",
        "ocp4-prep-five-compute-01",
        "ocp4-two-infra-07",
    ],
)
def test_upi_hostnames_classify_as_upi(name: str) -> None:
    assert _classify(name) == {InstallationType.UPI}


@pytest.mark.parametrize(
    "name",
    [
        "random-server-0009",
        "ocp4-stone-01",  # contains "one" but names no site
        "ocp4-prod-infra-01",  # no site token at all
        "some-unmanaged-box",
        "",
    ],
)
def test_unrecognized_hostnames_match_nothing(name: str) -> None:
    assert _classify(name) == set()


def test_hosted_cluster_and_upi_patterns_are_mutually_exclusive() -> None:
    """Both families share the `ocp4-` prefix, so overlapping patterns
    would make the result depend on rule ordering rather than on the
    hostname. Every name must be claimed by at most one rule.
    """
    names = [
        "ocp4-hypershift-five-01",
        "ocp4-hypershift-data-five-02",
        "ocp-dell-r660-five-128c-1024gb-FCH1234567",
        "ocp4-five-compute-01",
        "ocp4-one-control-plane-02",
        "ocp4-prod-one-infra-01",
    ]
    for name in names:
        matched = [r for r in default_system_rules() if re.compile(r.pattern, re.I).match(name)]
        assert len(matched) == 1, f"{name} matched {[r.name for r in matched]}"
