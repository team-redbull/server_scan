"""Unit tests (no I/O) for `app.application.services.health_policy_service`.

Only the pure validation helpers (`validate_policy_write`,
`validate_system_field_lock`) are exercised here — `HealthPolicyService`
itself talks to Mongo (policy repo + server repo) and is covered by
`tests/integration/test_health_policy_repository.py` and
`tests/api/test_health_policies.py` instead.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.application.services.health_policy_service import (
    validate_policy_write,
    validate_system_field_lock,
)
from app.domain.enums import HealthSeverity
from app.domain.models.health_policy import EvidenceField, HealthPolicy, PolicyScope
from app.domain.services.health.conditions import Condition
from app.domain.services.health.metrics import MetricDef, MetricRegistry, MetricType
from app.errors import (
    ConditionInvalidError,
    MetricOperatorMismatchError,
    TemplateInvalidError,
    UnknownMetricError,
    ValidationAppError,
)

NOW = datetime.now(UTC)


def _registry() -> MetricRegistry:
    r = MetricRegistry()
    r.register(
        MetricDef(
            name="connectivity.fabric_paths_down",
            type=MetricType.INT,
            category="connectivity",
            description="",
            resolver=lambda f: f.get("connectivity.fabric_paths_down", 0),
        )
    )
    r.register(
        MetricDef(
            name="network.interface_link_states",
            type=MetricType.LIST_STRING,
            category="network",
            description="",
            resolver=lambda f: f.get("network.interface_link_states", []),
        )
    )
    return r


REGISTRY = _registry()


def _policy(
    *,
    source: str,
    priority: int,
    scope: PolicyScope | None = None,
    condition: Condition | None = None,
    evidence: list | None = None,
    message_template: str = "ok",
    system: bool = False,
    policy_key: str = "test-key",
) -> HealthPolicy:
    return HealthPolicy(
        _id="hpol_test",
        name="test-policy",
        policy_key=policy_key,
        category="connectivity",
        severity=HealthSeverity.WARNING,
        condition=condition
        or Condition(metric="connectivity.fabric_paths_down", operator="GTE", value=1),
        evidence=evidence or [],
        message_template=message_template,
        scope=scope or PolicyScope(),
        source=source,
        priority=priority,
        system=system,
        created_at=NOW,
        updated_at=NOW,
    )


# --- Condition validation ---


def test_valid_condition_and_template_pass() -> None:
    policy = _policy(source="GLOBAL_CUSTOM", priority=200, message_template="ok")
    validate_policy_write(policy, registry=REGISTRY)  # should not raise


def test_unknown_metric_raises_unknown_metric_error() -> None:
    policy = _policy(
        source="GLOBAL_CUSTOM",
        priority=200,
        condition=Condition(metric="does.not.exist", operator="EQ", value=1),
    )
    with pytest.raises(UnknownMetricError):
        validate_policy_write(policy, registry=REGISTRY)


def test_operator_type_mismatch_raises_metric_operator_mismatch_error() -> None:
    # GT is scalar-only; network.interface_link_states is LIST_STRING.
    policy = _policy(
        source="GLOBAL_CUSTOM",
        priority=200,
        condition=Condition(metric="network.interface_link_states", operator="GT", value=1),
    )
    with pytest.raises(MetricOperatorMismatchError):
        validate_policy_write(policy, registry=REGISTRY)


def test_condition_too_deep_raises_condition_invalid_error() -> None:
    leaf = Condition(metric="connectivity.fabric_paths_down", operator="GTE", value=1)
    nested = leaf
    for _ in range(10):  # exceeds MAX_CONDITION_DEPTH
        nested = Condition(all_of=[nested])
    policy = _policy(source="GLOBAL_CUSTOM", priority=200, condition=nested)
    with pytest.raises(ConditionInvalidError):
        validate_policy_write(policy, registry=REGISTRY)


# --- Template validation ---


def test_template_referencing_undeclared_field_raises_template_invalid_error() -> None:
    policy = _policy(
        source="GLOBAL_CUSTOM", priority=200, message_template="{undeclared_field}", evidence=[]
    )
    with pytest.raises(TemplateInvalidError):
        validate_policy_write(policy, registry=REGISTRY)


def test_template_referencing_declared_evidence_key_passes() -> None:
    policy = _policy(
        source="GLOBAL_CUSTOM",
        priority=200,
        message_template="{down} paths down",
        evidence=[EvidenceField(key="down", metric="connectivity.fabric_paths_down")],
    )
    validate_policy_write(policy, registry=REGISTRY)  # should not raise


# --- Source / scope coherence ---


def test_site_custom_without_site_id_raises() -> None:
    policy = _policy(source="SITE_CUSTOM", priority=500, scope=PolicyScope())
    with pytest.raises(ValidationAppError):
        validate_policy_write(policy, registry=REGISTRY)


def test_site_custom_with_site_id_passes() -> None:
    policy = _policy(source="SITE_CUSTOM", priority=500, scope=PolicyScope(site_id="site_a"))
    validate_policy_write(policy, registry=REGISTRY)  # should not raise


def test_manager_custom_without_manager_type_raises() -> None:
    policy = _policy(source="MANAGER_CUSTOM", priority=400, scope=PolicyScope())
    with pytest.raises(ValidationAppError):
        validate_policy_write(policy, registry=REGISTRY)


def test_vendor_custom_without_vendor_raises() -> None:
    policy = _policy(source="VENDOR_CUSTOM", priority=300, scope=PolicyScope())
    with pytest.raises(ValidationAppError):
        validate_policy_write(policy, registry=REGISTRY)


def test_global_custom_with_nonempty_scope_raises() -> None:
    policy = _policy(source="GLOBAL_CUSTOM", priority=200, scope=PolicyScope(vendor="dell"))
    with pytest.raises(ValidationAppError):
        validate_policy_write(policy, registry=REGISTRY)


def test_global_custom_with_empty_scope_passes() -> None:
    policy = _policy(source="GLOBAL_CUSTOM", priority=200, scope=PolicyScope())
    validate_policy_write(policy, registry=REGISTRY)  # should not raise


def test_system_default_with_empty_scope_passes() -> None:
    policy = _policy(source="SYSTEM_DEFAULT", priority=100, scope=PolicyScope(), system=True)
    validate_policy_write(policy, registry=REGISTRY)  # should not raise


# --- System field lock ---


def test_system_policy_enabled_only_update_passes() -> None:
    existing = _policy(source="SYSTEM_DEFAULT", priority=100, system=True)
    validate_system_field_lock(existing=existing, updates={"enabled": False})  # should not raise


def test_system_policy_other_field_update_raises() -> None:
    existing = _policy(source="SYSTEM_DEFAULT", priority=100, system=True)
    with pytest.raises(ValidationAppError):
        validate_system_field_lock(existing=existing, updates={"priority": 150})


def test_system_policy_mixed_update_raises() -> None:
    existing = _policy(source="SYSTEM_DEFAULT", priority=100, system=True)
    with pytest.raises(ValidationAppError):
        validate_system_field_lock(existing=existing, updates={"enabled": False, "priority": 150})


def test_non_system_policy_any_update_passes() -> None:
    existing = _policy(source="GLOBAL_CUSTOM", priority=200, system=False)
    validate_system_field_lock(
        existing=existing, updates={"priority": 250, "enabled": False}
    )  # should not raise
