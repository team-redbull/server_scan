"""Unit test (no I/O) for `default_system_rules` — pure object
construction, no Mongo connection involved.
"""

from __future__ import annotations

from app.domain.enums import InstallationType, Vendor
from app.domain.models.classification_rule import PRIORITY_BANDS
from app.infrastructure.mongodb.classification_rule_repository import default_system_rules


def test_returns_the_four_spec_acceptance_rules() -> None:
    rules = default_system_rules()
    assert len(rules) == 4
    assert len({r.id for r in rules}) == 4  # ids are unique
    assert len({r.name for r in rules}) == 4  # names are unique


def test_system_default_rules_are_unscoped_and_locked() -> None:
    rules = {r.name: r for r in default_system_rules()}

    hosted = rules["system-default-hosted-cluster"]
    assert hosted.source == "SYSTEM_DEFAULT"
    assert hosted.system is True
    assert hosted.enabled is True
    assert hosted.priority == 100
    assert hosted.field == "name"
    assert hosted.pattern == r"^ocp-.*"
    assert hosted.installation_type == InstallationType.HOSTED_CLUSTER
    assert hosted.scope.vendor is None
    assert hosted.scope.manager_type is None
    assert hosted.scope.site_id is None

    upi = rules["system-default-upi"]
    assert upi.source == "SYSTEM_DEFAULT"
    assert upi.system is True
    assert upi.priority == 100
    assert upi.pattern == r"^upi-.*"
    assert upi.installation_type == InstallationType.UPI


def test_dell_vendor_rules_are_scoped_and_editable() -> None:
    rules = {r.name: r for r in default_system_rules()}

    dell_hosted = rules["dell-vendor-hosted-cluster"]
    assert dell_hosted.source == "VENDOR_CUSTOM"
    assert dell_hosted.system is False  # editable/deletable, unlike SYSTEM_DEFAULT
    assert dell_hosted.priority == 300
    assert dell_hosted.pattern == r"^ocp-dell-.*"
    assert dell_hosted.scope.vendor == Vendor.DELL
    assert dell_hosted.installation_type == InstallationType.HOSTED_CLUSTER

    dell_upi = rules["dell-vendor-upi"]
    assert dell_upi.source == "VENDOR_CUSTOM"
    assert dell_upi.system is False
    assert dell_upi.priority == 300
    assert dell_upi.pattern == r"^upi-dell-.*"
    assert dell_upi.scope.vendor == Vendor.DELL
    assert dell_upi.installation_type == InstallationType.UPI


def test_every_rule_priority_falls_within_its_own_band() -> None:
    for rule in default_system_rules():
        low, high = PRIORITY_BANDS[rule.source]
        assert low <= rule.priority <= high
