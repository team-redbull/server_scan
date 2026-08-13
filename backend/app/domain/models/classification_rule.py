"""The `classification_rules` collection.

Distinct from `app.domain.models.classification.Classification` (the small
*result* embedded on a `Server` document, already built in slice 1) — this
is the rule that produces that result. Kept in a separate module rather
than added to `classification.py` because a rule and a classification
result have entirely different lifecycles: rules are authored/edited by
operators, results are computed and overwritten by the engine.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.domain.enums import InstallationType, ManagerType, Vendor

# Priority bands per the spec: SITE_CUSTOM 500-599, MANAGER_CUSTOM 400-499,
# VENDOR_CUSTOM 300-399, GLOBAL_CUSTOM 200-299, SYSTEM_DEFAULT 100-199.
# A rule's `priority` must fall in its `source`'s band — enforced at write
# time by the classification service, not here (this is a data model, not
# a validator; the service layer owns cross-field business rules so the
# same check isn't duplicated in every code path that constructs one).
PRIORITY_BANDS: dict[str, tuple[int, int]] = {
    "SITE_CUSTOM": (500, 599),
    "MANAGER_CUSTOM": (400, 499),
    "VENDOR_CUSTOM": (300, 399),
    "GLOBAL_CUSTOM": (200, 299),
    "SYSTEM_DEFAULT": (100, 199),
}

# The only fields a classification rule may match against. Deliberately a
# closed set rather than an arbitrary dotted path into the document — this
# is what keeps a rule author from regexing over raw provider payloads or
# internal-only fields, and keeps the UI's field picker finite.
CLASSIFIABLE_FIELDS = frozenset({"name", "hostname", "serial", "model", "site_id"})


class RuleScope(BaseModel):
    vendor: Vendor | None = None
    manager_type: ManagerType | None = None
    site_id: str | None = None

    def specificity(self) -> int:
        """Powers of two so more-specific scopes strictly outrank less
        specific ones regardless of how many dimensions are set — see
        `app.domain.services.classification`'s resolution algorithm.
        """
        return (
            (4 if self.site_id is not None else 0)
            + (2 if self.manager_type is not None else 0)
            + (1 if self.vendor is not None else 0)
        )

    def matches(
        self, *, vendor: Vendor, manager_type: ManagerType | None, site_id: str | None
    ) -> bool:
        if self.vendor is not None and self.vendor != vendor:
            return False
        if self.manager_type is not None and self.manager_type != manager_type:
            return False
        return not (self.site_id is not None and self.site_id != site_id)


class RuleFlags(BaseModel):
    ignore_case: bool = True
    multiline: bool = False
    dotall: bool = False


class RuleStats(BaseModel):
    match_count: int = 0
    last_matched_at: datetime | None = None
    timeout_count: int = 0
    quarantined: bool = False


class ClassificationRule(BaseModel):
    id: str = Field(alias="_id")
    name: str
    description: str = ""
    enabled: bool = True
    system: bool = False

    installation_type: InstallationType
    scope: RuleScope = Field(default_factory=RuleScope)
    field: str
    pattern: str
    flags: RuleFlags = Field(default_factory=RuleFlags)

    source: str  # SITE_CUSTOM | MANAGER_CUSTOM | VENDOR_CUSTOM | GLOBAL_CUSTOM | SYSTEM_DEFAULT
    priority: int
    order: int = 0

    stats: RuleStats = Field(default_factory=RuleStats)

    revision: int = 1
    created_at: datetime
    updated_at: datetime
    created_by: str | None = None
    updated_by: str | None = None

    model_config = {"populate_by_name": True}
