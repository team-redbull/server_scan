"""API request/response schemas for `/api/v1/health-policies` and `/api/v1/health-metrics`.

Same rationale as `app.api.v1.schemas` (`ServerSummary`/`ServerDetail`):
the top-level request/response models here are dedicated, never the
domain `HealthPolicy` returned/accepted as-is — `HealthPolicy.id` is
aliased to `_id` for MongoDB, and a dedicated response model is the seam
that stops a future storage-only field from silently becoming public API
surface. Nested value-object-shaped pieces (`Condition`, `EvidenceField`,
`PolicyScope`, `PolicyStats`) are reused directly from the domain layer,
the same way `ServerDetail` reuses `Hardware`/`NetworkInfo`/etc. — they
are already the exact wire shape a client needs, and `Condition` in
particular is a validated recursive grammar that would be pure
duplication to redeclare here.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from app.domain.enums import HealthSeverity
from app.domain.models.health_policy import EvidenceField, HealthPolicy, PolicyScope, PolicyStats
from app.domain.services.health.conditions import Condition
from app.domain.services.health.metrics import MetricType


class HealthPolicyResponse(BaseModel):
    """The public representation of one health policy."""

    id: str
    name: str
    description: str
    enabled: bool
    system: bool
    policy_key: str
    mode: str
    category: str
    severity: HealthSeverity
    condition: Condition
    evidence: list[EvidenceField]
    message_template: str
    scope: PolicyScope
    source: str
    priority: int
    order: int
    stats: PolicyStats
    revision: int
    created_at: datetime
    updated_at: datetime
    created_by: str | None
    updated_by: str | None

    @classmethod
    def from_policy(cls, policy: HealthPolicy) -> HealthPolicyResponse:
        """
        Build the response schema from a domain health policy.

        Args:
            policy (HealthPolicy): The stored policy.

        Returns:
            HealthPolicyResponse: The response model.
        """
        return cls(
            id=policy.id,
            name=policy.name,
            description=policy.description,
            enabled=policy.enabled,
            system=policy.system,
            policy_key=policy.policy_key,
            mode=policy.mode,
            category=policy.category,
            severity=policy.severity,
            condition=policy.condition,
            evidence=policy.evidence,
            message_template=policy.message_template,
            scope=policy.scope,
            source=policy.source,
            priority=policy.priority,
            order=policy.order,
            stats=policy.stats,
            revision=policy.revision,
            created_at=policy.created_at,
            updated_at=policy.updated_at,
            created_by=policy.created_by,
            updated_by=policy.updated_by,
        )


class HealthPolicyListResponse(BaseModel):
    """The full list of health policies."""

    items: list[HealthPolicyResponse]


class HealthMetricResponse(BaseModel):
    """One entry in the metric registry, for a future condition-builder UI.

    Carries the same fields `MetricDef` does, minus `resolver`, which is a
    Python callable and has no business crossing the API boundary.
    """

    name: str
    type: MetricType
    category: str
    description: str
    enum_values: list[str] | None
    provider: str


class HealthMetricListResponse(BaseModel):
    """The full list of metric definitions the health engine can evaluate."""

    items: list[HealthMetricResponse]
