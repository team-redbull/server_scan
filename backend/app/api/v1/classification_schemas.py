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

from pydantic import BaseModel

from app.domain.enums import InstallationType, ManagerType, Vendor
from app.domain.models.classification_rule import ClassificationRule


class RuleScopeSchema(BaseModel):
    """The vendor/manager-type/site a classification rule is restricted to."""

    vendor: Vendor | None = None
    manager_type: ManagerType | None = None
    site_id: str | None = None


class RuleFlagsSchema(BaseModel):
    """The regex flags a classification rule's pattern is compiled with."""

    ignore_case: bool = True
    multiline: bool = False
    dotall: bool = False


class RuleStatsSchema(BaseModel):
    """A classification rule's accumulated timeout/quarantine counters."""

    last_matched_at: datetime | None
    timeout_count: int
    quarantined: bool


class ClassificationRuleResponse(BaseModel):
    """The public representation of one classification rule."""

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
        """
        Build the response schema from a domain classification rule.

        Args:
            rule (ClassificationRule): The stored rule.

        Returns:
            ClassificationRuleResponse: The response model.
        """
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
    """The full list of classification rules."""

    items: list[ClassificationRuleResponse]
