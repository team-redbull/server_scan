"""MongoDB implementation of the classification rule repository.

Small, human-curated collection (dozens to low hundreds of rules, per the
platform spec) — same rationale as `site_repository.py`/`manager_repository.
py`: no cursor pagination, `list_all()` returns everything. The one thing
that *does* matter here is sort order: `list_all(enabled_only=True)` sorts
by `(priority DESC, order ASC, _id ASC)`, which is a prefix match against
the `enabled_priority_order_id` compound index declared in
`app.infrastructure.mongodb.indexes` — filtering on the leading `enabled`
field plus sorting on the exact remaining index key order is what keeps
this an IXSCAN rather than an in-memory sort (verified in
`tests/integration/test_classification_rule_repository.py` via `.explain()`).
The final specificity tiebreak in the real resolution order is computed in
Python by `app.domain.services.classification.classify` — Mongo has no way
to express `RuleScope.specificity()` as an index key.
"""

from __future__ import annotations

from typing import Any

from pymongo import ASCENDING, DESCENDING
from pymongo.asynchronous.collection import AsyncCollection

from app.domain.enums import InstallationType, SiteCode
from app.domain.models.classification_rule import ClassificationRule, RuleScope
from app.infrastructure.mongodb.client import MongoClientHolder
from app.infrastructure.mongodb.indexes import CLASSIFICATION_RULES_COLLECTION
from app.utils.ids import new_id
from app.utils.timeutil import utcnow

_Document = dict[str, Any]


class MongoClassificationRuleRepository:
    def __init__(self, mongo: MongoClientHolder) -> None:
        self._mongo = mongo

    @property
    def _collection(self) -> AsyncCollection[_Document]:
        return self._mongo.db[CLASSIFICATION_RULES_COLLECTION]

    async def upsert(self, rule: ClassificationRule) -> ClassificationRule:
        """Replace-or-insert by `_id`. Raises `pymongo.errors.
        DuplicateKeyError` (uncaught) on a `name` collision with another
        rule — the API layer is what turns that into a 409, this
        repository just surfaces the driver's own error.
        """
        doc = rule.model_dump(by_alias=True, mode="json")
        await self._collection.replace_one({"_id": rule.id}, doc, upsert=True)
        return rule

    async def get_by_id(self, rule_id: str) -> ClassificationRule | None:
        doc = await self._collection.find_one({"_id": rule_id})
        if doc is None:
            return None
        return ClassificationRule.model_validate(doc)

    async def get_by_name(self, name: str) -> ClassificationRule | None:
        doc = await self._collection.find_one({"name": name})
        if doc is None:
            return None
        return ClassificationRule.model_validate(doc)

    async def list_all(self, *, enabled_only: bool = False) -> list[ClassificationRule]:
        query: dict[str, object] = {"enabled": True} if enabled_only else {}
        docs = await (
            self._collection.find(query)
            .sort([("priority", DESCENDING), ("order", ASCENDING), ("_id", ASCENDING)])
            .to_list(length=None)
        )
        return [ClassificationRule.model_validate(doc) for doc in docs]

    async def delete(self, rule_id: str) -> bool:
        result = await self._collection.delete_one({"_id": rule_id})
        return result.deleted_count > 0


# The site token, as one alternation, built from `SiteCode` so adding a
# site to the enum can't leave these patterns silently behind.
_SITE_ALTERNATION = "|".join(member.value for member in SiteCode)

# Each pattern is fully anchored and names the exact shape it accepts,
# rather than a loose prefix. That matters because HOSTED_CLUSTER and UPI
# hostnames share the `ocp4-` prefix — only a later token tells them
# apart — so a prefix rule like the old `^ocp-.*` would match both and
# leave the outcome depending on rule ordering. Anchored, mutually
# exclusive patterns make the classification independent of order.
_HYPERSHIFT_PATTERN = rf"^ocp4-hypershift(-data)?-({_SITE_ALTERNATION})-\d+$"
_HARDWARE_SPEC_PATTERN = rf"^ocp-[a-z]+-[a-z0-9]+-({_SITE_ALTERNATION})-\d+c-\d+gb-.+$"
_UPI_PATTERN = rf"^ocp4-([a-z]+-)?({_SITE_ALTERNATION})-(compute|control-plane|infra)-\d+$"


def default_system_rules() -> list[ClassificationRule]:
    """The three unscoped SYSTEM_DEFAULT rules that cover this estate's
    real hostname conventions, as ready-to-persist `ClassificationRule`s:
    two shapes of hosted cluster and one of UPI.

    All three are `system=True` (locked to enabled-only edits after
    creation) because they encode a naming convention that holds fleet-
    wide, not a per-vendor preference. Vendor-scoped rules are exactly the
    kind of thing an operator adds on top through the UI, at a higher
    priority band — see `PRIORITY_BANDS`.

    Deliberately NOT wired into app startup or the seed script here —
    that's a separate integration step (see this module's caller). A
    caller seeds these by calling `MongoClassificationRuleRepository.
    upsert()` once per returned rule; re-running this function generates
    fresh ids each time; two calls to it plus the collection's unique
    `name` index means a second seed attempt fails loudly
    (`DuplicateKeyError`) rather than silently double-inserting, which is
    the intended "call this exactly once" contract.
    """
    now = utcnow()
    return [
        ClassificationRule(
            id=new_id("classification_rule"),
            name="system-default-hypershift-hosted-cluster",
            description=(
                "Hosted control planes: ocp4-hypershift-<site>-NN and "
                "ocp4-hypershift-data-<site>-NN."
            ),
            enabled=True,
            system=True,
            installation_type=InstallationType.HOSTED_CLUSTER,
            scope=RuleScope(),
            field="name",
            pattern=_HYPERSHIFT_PATTERN,
            source="SYSTEM_DEFAULT",
            priority=100,
            order=0,
            created_at=now,
            updated_at=now,
        ),
        ClassificationRule(
            id=new_id("classification_rule"),
            name="system-default-hardware-hosted-cluster",
            description=(
                "Hosted-cluster nodes named after their hardware spec: "
                "ocp-<vendor>-<model>-<site>-<cores>c-<memory>gb-<serial>."
            ),
            enabled=True,
            system=True,
            installation_type=InstallationType.HOSTED_CLUSTER,
            scope=RuleScope(),
            field="name",
            pattern=_HARDWARE_SPEC_PATTERN,
            source="SYSTEM_DEFAULT",
            priority=100,
            order=1,
            created_at=now,
            updated_at=now,
        ),
        ClassificationRule(
            id=new_id("classification_rule"),
            name="system-default-upi",
            description=(
                "User-provisioned infrastructure: ocp4-[<env>-]<site>-<role>-NN, "
                "where role is compute, control-plane or infra."
            ),
            enabled=True,
            system=True,
            installation_type=InstallationType.UPI,
            scope=RuleScope(),
            field="name",
            pattern=_UPI_PATTERN,
            source="SYSTEM_DEFAULT",
            priority=100,
            order=2,
            created_at=now,
            updated_at=now,
        ),
    ]
