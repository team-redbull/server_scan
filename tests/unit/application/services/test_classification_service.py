"""Unit tests (no I/O) for `app.application.services.classification_service`.

Only `validate_rule_write` is exercised here — `ClassificationService`
itself talks to Mongo (rule repo + servers collection) and is covered by
`tests/integration/test_classification_rule_repository.py` and
`tests/api/test_classification_rules.py` instead. `RegexModuleEngine` is
used directly (not mocked): it's pure CPU-bound computation, not I/O (see
its own module docstring), so exercising the real engine here is still a
"no I/O" unit test.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.application.services.classification_service import validate_rule_write
from app.domain.enums import InstallationType, ManagerType, Vendor
from app.domain.models.classification_rule import ClassificationRule, RuleScope
from app.domain.services.regex_engine import RegexModuleEngine
from app.errors import RegexUnsafeAppError, RuleScopeInvalidError, ValidationAppError

NOW = datetime.now(UTC)
ENGINE = RegexModuleEngine(max_pattern_length=200, match_timeout_seconds=0.25)


def _rule(
    *,
    source: str,
    priority: int,
    scope: RuleScope | None = None,
    field: str = "name",
    pattern: str = r"^ocp-.*",
    system: bool = False,
) -> ClassificationRule:
    return ClassificationRule(
        _id="crul_test",
        name="test-rule",
        installation_type=InstallationType.HOSTED_CLUSTER,
        scope=scope or RuleScope(),
        field=field,
        pattern=pattern,
        source=source,
        priority=priority,
        system=system,
        created_at=NOW,
        updated_at=NOW,
    )


# --- Priority band checks ---


@pytest.mark.parametrize(
    ("source", "priority", "scope"),
    [
        ("SITE_CUSTOM", 500, RuleScope(site_id="site_a")),
        ("SITE_CUSTOM", 599, RuleScope(site_id="site_a")),
        ("MANAGER_CUSTOM", 400, RuleScope(manager_type=ManagerType.OPENMANAGE)),
        ("MANAGER_CUSTOM", 499, RuleScope(manager_type=ManagerType.OPENMANAGE)),
        ("VENDOR_CUSTOM", 300, RuleScope(vendor=Vendor.DELL)),
        ("VENDOR_CUSTOM", 399, RuleScope(vendor=Vendor.DELL)),
        ("GLOBAL_CUSTOM", 200, RuleScope()),
        ("GLOBAL_CUSTOM", 299, RuleScope()),
        ("SYSTEM_DEFAULT", 100, RuleScope()),
        ("SYSTEM_DEFAULT", 199, RuleScope()),
    ],
)
def test_priority_within_band_passes(source: str, priority: int, scope: RuleScope) -> None:
    rule = _rule(source=source, priority=priority, scope=scope)
    validate_rule_write(rule, ENGINE)  # should not raise


@pytest.mark.parametrize(
    ("source", "priority", "scope"),
    [
        ("SITE_CUSTOM", 499, RuleScope(site_id="site_a")),
        ("SITE_CUSTOM", 600, RuleScope(site_id="site_a")),
        ("MANAGER_CUSTOM", 399, RuleScope(manager_type=ManagerType.OPENMANAGE)),
        ("MANAGER_CUSTOM", 500, RuleScope(manager_type=ManagerType.OPENMANAGE)),
        ("VENDOR_CUSTOM", 299, RuleScope(vendor=Vendor.DELL)),
        ("VENDOR_CUSTOM", 400, RuleScope(vendor=Vendor.DELL)),
        ("GLOBAL_CUSTOM", 199, RuleScope()),
        ("GLOBAL_CUSTOM", 300, RuleScope()),
        ("SYSTEM_DEFAULT", 99, RuleScope()),
        ("SYSTEM_DEFAULT", 200, RuleScope()),
    ],
)
def test_priority_out_of_band_raises(source: str, priority: int, scope: RuleScope) -> None:
    rule = _rule(source=source, priority=priority, scope=scope)
    with pytest.raises(RuleScopeInvalidError):
        validate_rule_write(rule, ENGINE)


def test_unknown_source_raises() -> None:
    rule = _rule(source="NOT_A_REAL_SOURCE", priority=100, scope=RuleScope())
    with pytest.raises(RuleScopeInvalidError):
        validate_rule_write(rule, ENGINE)


# --- Source / scope coherence ---


def test_site_custom_without_site_id_raises() -> None:
    rule = _rule(source="SITE_CUSTOM", priority=500, scope=RuleScope())
    with pytest.raises(RuleScopeInvalidError):
        validate_rule_write(rule, ENGINE)


def test_manager_custom_without_manager_type_raises() -> None:
    rule = _rule(source="MANAGER_CUSTOM", priority=400, scope=RuleScope())
    with pytest.raises(RuleScopeInvalidError):
        validate_rule_write(rule, ENGINE)


def test_vendor_custom_without_vendor_raises() -> None:
    rule = _rule(source="VENDOR_CUSTOM", priority=300, scope=RuleScope())
    with pytest.raises(RuleScopeInvalidError):
        validate_rule_write(rule, ENGINE)


def test_global_custom_with_nonempty_scope_raises() -> None:
    rule = _rule(source="GLOBAL_CUSTOM", priority=200, scope=RuleScope(vendor=Vendor.DELL))
    with pytest.raises(RuleScopeInvalidError):
        validate_rule_write(rule, ENGINE)


def test_system_default_with_nonempty_scope_raises() -> None:
    rule = _rule(
        source="SYSTEM_DEFAULT", priority=100, scope=RuleScope(site_id="site_a"), system=True
    )
    with pytest.raises(RuleScopeInvalidError):
        validate_rule_write(rule, ENGINE)


def test_system_default_cannot_be_created_via_api() -> None:
    rule = _rule(source="SYSTEM_DEFAULT", priority=100, scope=RuleScope())
    with pytest.raises(RuleScopeInvalidError):
        validate_rule_write(rule, ENGINE, is_create=True)


def test_system_default_update_with_valid_scope_passes() -> None:
    # Not a create -- this is the shape a system rule's `enabled` toggle
    # takes: same source/priority/scope, only `enabled` differs.
    rule = _rule(source="SYSTEM_DEFAULT", priority=100, scope=RuleScope(), system=True)
    validate_rule_write(rule, ENGINE, is_create=False)  # should not raise


# --- Field whitelist ---


def test_field_outside_classifiable_fields_raises_validation_error() -> None:
    rule = _rule(source="GLOBAL_CUSTOM", priority=200, scope=RuleScope(), field="not_a_real_field")
    with pytest.raises(ValidationAppError):
        validate_rule_write(rule, ENGINE)


@pytest.mark.parametrize("field", ["name", "hostname", "serial", "model", "site_id"])
def test_every_classifiable_field_is_accepted(field: str) -> None:
    rule = _rule(source="GLOBAL_CUSTOM", priority=200, scope=RuleScope(), field=field)
    validate_rule_write(rule, ENGINE)  # should not raise


# --- Pattern safety ---


def test_unsafe_pattern_raises_regex_unsafe_app_error() -> None:
    rule = _rule(source="GLOBAL_CUSTOM", priority=200, scope=RuleScope(), pattern="(unclosed")
    with pytest.raises(RegexUnsafeAppError):
        validate_rule_write(rule, ENGINE)


def test_safe_pattern_passes() -> None:
    rule = _rule(source="GLOBAL_CUSTOM", priority=200, scope=RuleScope(), pattern=r"^ocp-dell-.*$")
    validate_rule_write(rule, ENGINE)  # should not raise
