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

from app.domain.enums import InstallationType, Vendor
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


def default_system_rules() -> list[ClassificationRule]:
    """The platform spec's own acceptance scenario (spec §70), returned as
    ready-to-persist `ClassificationRule`s: two unscoped SYSTEM_DEFAULT
    rules, plus two Dell-scoped VENDOR_CUSTOM rules that outrank them for
    Dell servers.

    Deliberately NOT wired into app startup or the seed script here —
    that's a separate integration step (see this module's caller). A
    caller seeds these by calling `MongoClassificationRuleRepository.
    upsert()` once per returned rule; re-running this function generates
    fresh ids each time; two calls to it plus the collection's unique
    `name` index means a second seed attempt fails loudly
    (`DuplicateKeyError`) rather than silently double-inserting, which is
    the intended "call this exactly once" contract.

    Only the two SYSTEM_DEFAULT rules get `system=True` (and are therefore
    locked to enabled-only edits after creation) — the Dell VENDOR_CUSTOM
    rules are ordinary, editable/deletable rules that merely happen to
    ship pre-seeded; see `PRIORITY_BANDS`'s docstring and the spec's own
    distinction between "system" and "vendor default".
    """
    now = utcnow()
    return [
        ClassificationRule(
            id=new_id("classification_rule"),
            name="system-default-hosted-cluster",
            description="Default OpenShift hosted-cluster naming convention (ocp-*).",
            enabled=True,
            system=True,
            installation_type=InstallationType.HOSTED_CLUSTER,
            scope=RuleScope(),
            field="name",
            pattern=r"^ocp-.*",
            source="SYSTEM_DEFAULT",
            priority=100,
            order=0,
            created_at=now,
            updated_at=now,
        ),
        ClassificationRule(
            id=new_id("classification_rule"),
            name="system-default-upi",
            description="Default user-provisioned-infrastructure naming convention (upi-*).",
            enabled=True,
            system=True,
            installation_type=InstallationType.UPI,
            scope=RuleScope(),
            field="name",
            pattern=r"^upi-.*",
            source="SYSTEM_DEFAULT",
            priority=100,
            order=0,
            created_at=now,
            updated_at=now,
        ),
        ClassificationRule(
            id=new_id("classification_rule"),
            name="dell-vendor-hosted-cluster",
            description="Dell-specific OpenShift hosted-cluster naming convention (ocp-dell-*).",
            enabled=True,
            system=False,
            installation_type=InstallationType.HOSTED_CLUSTER,
            scope=RuleScope(vendor=Vendor.DELL),
            field="name",
            pattern=r"^ocp-dell-.*",
            source="VENDOR_CUSTOM",
            priority=300,
            order=0,
            created_at=now,
            updated_at=now,
        ),
        ClassificationRule(
            id=new_id("classification_rule"),
            name="dell-vendor-upi",
            description="Dell-specific user-provisioned-infrastructure naming convention "
            "(upi-dell-*).",
            enabled=True,
            system=False,
            installation_type=InstallationType.UPI,
            scope=RuleScope(vendor=Vendor.DELL),
            field="name",
            pattern=r"^upi-dell-.*",
            source="VENDOR_CUSTOM",
            priority=300,
            order=0,
            created_at=now,
            updated_at=now,
        ),
    ]
