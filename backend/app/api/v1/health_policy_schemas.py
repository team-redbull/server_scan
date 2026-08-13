"""API request/response schemas for `/api/v1/health-policies` and
`/api/v1/health-metrics`.

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

from pydantic import BaseModel, Field

from app.domain.enums import HealthSeverity
from app.domain.models.health_policy import EvidenceField, HealthPolicy, PolicyScope, PolicyStats
from app.domain.services.health.conditions import Condition
from app.domain.services.health.metrics import MetricType


class HealthPolicyCreate(BaseModel):
    """`system` is deliberately absent — system-default policies are
    seeded (`app.domain.services.health.health_policy_defaults.
    default_system_policies`), never created through this endpoint, so a
    caller can never mint a policy that looks system-owned. `policy_key`
    is optional: when omitted, the API layer defaults it to the new
    policy's own generated id (see `HealthPolicy`'s module docstring on
    why that's the correct default for "just add a policy").
    """

    name: str
    description: str = ""
    enabled: bool = True
    policy_key: str | None = None
    mode: str = "EVALUATE"
    category: str
    severity: HealthSeverity
    condition: Condition
    evidence: list[EvidenceField] = Field(default_factory=list)
    message_template: str
    scope: PolicyScope = Field(default_factory=PolicyScope)
    source: str
    priority: int
    order: int = 0


class HealthPolicyUpdate(BaseModel):
    """Partial update — every field optional, only fields the caller sets
    are applied (`.model_dump(exclude_unset=True)` in the route). System
    policies are further restricted to `enabled`-only at the route/service
    layer (`app.application.services.health_policy_service.
    validate_system_field_lock`), not by this schema's shape.
    """

    name: str | None = None
    description: str | None = None
    enabled: bool | None = None
    policy_key: str | None = None
    mode: str | None = None
    category: str | None = None
    severity: HealthSeverity | None = None
    condition: Condition | None = None
    evidence: list[EvidenceField] | None = None
    message_template: str | None = None
    scope: PolicyScope | None = None
    source: str | None = None
    priority: int | None = None
    order: int | None = None


class HealthPolicyResponse(BaseModel):
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
    items: list[HealthPolicyResponse]


class HealthPolicyPreviewRequest(BaseModel):
    """The draft policy under consideration, plus preview knobs.
    `policy_id` is set only when previewing an in-progress *edit* of an
    existing policy — it excludes that policy's currently-stored version
    from the "existing" set the draft is spliced into (see
    `HealthPolicyService.preview`), so a shadow race against itself never
    happens.
    """

    policy_id: str | None = None
    name: str
    description: str = ""
    enabled: bool = True
    policy_key: str | None = None
    mode: str = "EVALUATE"
    category: str
    severity: HealthSeverity
    condition: Condition
    evidence: list[EvidenceField] = Field(default_factory=list)
    message_template: str
    scope: PolicyScope = Field(default_factory=PolicyScope)
    source: str
    priority: int
    order: int = 0
    sample_size: int = Field(default=50, ge=1, le=500)
    max_scan: int = Field(default=5000, ge=1, le=20000)


class HealthPolicyPreviewSample(BaseModel):
    id: str
    name: str
    would_be_severity: HealthSeverity


class HealthPolicyPreviewResponse(BaseModel):
    matched_count: int
    truncated: bool
    sample: list[HealthPolicyPreviewSample]
    mode: str


class HealthMetricResponse(BaseModel):
    """One entry in the metric registry, exposed so a future condition
    builder UI can introspect what's available to reference — same
    fields `MetricDef` carries (minus `resolver`, which is a Python
    callable and has no business crossing the API boundary).
    """

    name: str
    type: MetricType
    category: str
    description: str
    enum_values: list[str] | None
    provider: str


class HealthMetricListResponse(BaseModel):
    items: list[HealthMetricResponse]
