"""API request/response schemas for `/api/v1/classification-rules`.

Not the domain `ClassificationRule` reused as-is — same reasoning as
`app.api.v1.schemas`'s `ServerSummary`/`ServerDetail` split (see that
module's docstring): `ClassificationRule.id` is aliased to `_id` for
MongoDB, server-assigned fields (`id`, `stats`, `revision`, `created_at`/
`updated_at`, `system`) have no business being caller-writable on a create
request, and a dedicated response schema is the seam that stops a future
storage-only field from silently leaking onto the public contract.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.domain.enums import InstallationType, ManagerType, Vendor
from app.domain.models.classification_rule import ClassificationRule


class RuleScopeSchema(BaseModel):
    vendor: Vendor | None = None
    manager_type: ManagerType | None = None
    site_id: str | None = None


class RuleFlagsSchema(BaseModel):
    ignore_case: bool = True
    multiline: bool = False
    dotall: bool = False


class RuleStatsSchema(BaseModel):
    match_count: int
    last_matched_at: datetime | None
    timeout_count: int
    quarantined: bool


class ClassificationRuleCreate(BaseModel):
    name: str
    description: str = ""
    enabled: bool = True
    installation_type: InstallationType
    scope: RuleScopeSchema = Field(default_factory=RuleScopeSchema)
    field: str
    pattern: str
    flags: RuleFlagsSchema = Field(default_factory=RuleFlagsSchema)
    source: str
    priority: int
    order: int = 0


class ClassificationRuleUpdate(BaseModel):
    """Every field optional — a PATCH sends only what it wants to change.
    `system`/`stats`/`revision`/timestamps are never caller-writable, same
    as on create.
    """

    name: str | None = None
    description: str | None = None
    enabled: bool | None = None
    installation_type: InstallationType | None = None
    scope: RuleScopeSchema | None = None
    field: str | None = None
    pattern: str | None = None
    flags: RuleFlagsSchema | None = None
    source: str | None = None
    priority: int | None = None
    order: int | None = None


class ClassificationRuleResponse(BaseModel):
    id: str
    name: str
    description: str
    enabled: bool
    system: bool
    installation_type: InstallationType
    scope: RuleScopeSchema
    field: str
    pattern: str
    flags: RuleFlagsSchema
    source: str
    priority: int
    order: int
    stats: RuleStatsSchema
    revision: int
    created_at: datetime
    updated_at: datetime
    created_by: str | None
    updated_by: str | None

    @classmethod
    def from_rule(cls, rule: ClassificationRule) -> ClassificationRuleResponse:
        return cls(
            id=rule.id,
            name=rule.name,
            description=rule.description,
            enabled=rule.enabled,
            system=rule.system,
            installation_type=rule.installation_type,
            scope=RuleScopeSchema(
                vendor=rule.scope.vendor,
                manager_type=rule.scope.manager_type,
                site_id=rule.scope.site_id,
            ),
            field=rule.field,
            pattern=rule.pattern,
            flags=RuleFlagsSchema(
                ignore_case=rule.flags.ignore_case,
                multiline=rule.flags.multiline,
                dotall=rule.flags.dotall,
            ),
            source=rule.source,
            priority=rule.priority,
            order=rule.order,
            stats=RuleStatsSchema(
                match_count=rule.stats.match_count,
                last_matched_at=rule.stats.last_matched_at,
                timeout_count=rule.stats.timeout_count,
                quarantined=rule.stats.quarantined,
            ),
            revision=rule.revision,
            created_at=rule.created_at,
            updated_at=rule.updated_at,
            created_by=rule.created_by,
            updated_by=rule.updated_by,
        )


class ClassificationRuleListResponse(BaseModel):
    items: list[ClassificationRuleResponse]


class ClassificationPreviewRequest(BaseModel):
    """Same shape as `ClassificationRuleCreate`, minus everything the
    preview algorithm doesn't need — a draft may be previewed before the
    author has settled on a `name`/`source`/`priority` at all. Only
    `field` and `pattern` are required.
    """

    installation_type: InstallationType | None = None
    scope: RuleScopeSchema = Field(default_factory=RuleScopeSchema)
    field: str
    pattern: str
    flags: RuleFlagsSchema = Field(default_factory=RuleFlagsSchema)


class ClassificationPreviewResponse(BaseModel):
    matched_count: int
    truncated: bool
    sample: list[dict[str, str]]
    mode: str
