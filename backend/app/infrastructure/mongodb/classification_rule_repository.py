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

from app.domain.enums import InstallationType
from app.domain.models.classification_rule import ClassificationRule, RuleScope
from app.domain.value_objects.site import SiteCatalog
from app.infrastructure.mongodb.client import MongoClientHolder
from app.infrastructure.mongodb.indexes import CLASSIFICATION_RULES_COLLECTION
from app.utils.ids import new_id
from app.utils.timeutil import utcnow

_Document = dict[str, Any]


class MongoClassificationRuleRepository:
    """MongoDB-backed store for classification rules."""

    def __init__(self, mongo: MongoClientHolder) -> None:
        """
        Store the shared Mongo client holder.

        Args:
            mongo (MongoClientHolder): The connected client holder.
        """
        self._mongo = mongo

    @property
    def _collection(self) -> AsyncCollection[_Document]:
        return self._mongo.db[CLASSIFICATION_RULES_COLLECTION]

    async def upsert(self, rule: ClassificationRule) -> ClassificationRule:
        """
        Replace-or-insert a rule by `_id`.

        Args:
            rule (ClassificationRule): The rule to persist.

        Returns:
            ClassificationRule: The same rule, for chaining.

        Raises:
            pymongo.errors.DuplicateKeyError: On a `name` collision with
                another rule — uncaught; the API layer turns it into a
                409, this repository just surfaces the driver's own error.
        """
        doc = rule.model_dump(by_alias=True, mode="json")
        await self._collection.replace_one({"_id": rule.id}, doc, upsert=True)
        return rule

    async def get_by_id(self, rule_id: str) -> ClassificationRule | None:
        """
        Look up one rule by its id.

        Args:
            rule_id (str): The rule's id.

        Returns:
            ClassificationRule | None: The rule, or None if not found.
        """
        doc = await self._collection.find_one({"_id": rule_id})
        if doc is None:
            return None
        return ClassificationRule.model_validate(doc)

    async def get_by_name(self, name: str) -> ClassificationRule | None:
        """
        Look up one rule by its unique name.

        Args:
            name (str): The rule's name.

        Returns:
            ClassificationRule | None: The rule, or None if not found.
        """
        doc = await self._collection.find_one({"name": name})
        if doc is None:
            return None
        return ClassificationRule.model_validate(doc)

    async def list_all(self, *, enabled_only: bool = False) -> list[ClassificationRule]:
        """
        List every rule, sorted by `(priority DESC, order ASC, _id ASC)`.

        Args:
            enabled_only (bool): If True, only return enabled rules.

        Returns:
            list[ClassificationRule]: All matching rules, in resolution
                order.
        """
        query: dict[str, object] = {"enabled": True} if enabled_only else {}
        docs = await (
            self._collection.find(query)
            .sort([("priority", DESCENDING), ("order", ASCENDING), ("_id", ASCENDING)])
            .to_list(length=None)
        )
        return [ClassificationRule.model_validate(doc) for doc in docs]

    async def delete(self, rule_id: str) -> bool:
        """
        Delete one rule by its id.

        Args:
            rule_id (str): The rule's id.

        Returns:
            bool: True if a rule was deleted, False if none matched.
        """
        result = await self._collection.delete_one({"_id": rule_id})
        return result.deleted_count > 0


# All four are broad prefix/substring catch-alls, none site-validated
# (2026-09-08, at the operator's request — this used to be four narrower,
# mutually-exclusive, site-anchored shapes; see git history for those).
# UPI's pattern matches every name, unconditionally (2026-09-10, also at
# the operator's request — it used to be `^ocp4`) — so classification is
# now genuinely ORDER-DEPENDENT, not just pattern-dependent: `order`
# below (0/1/2/3) is the *only* thing keeping a HOSTED_CLUSTER/MCE name
# from being swallowed by UPI, since UPI's own pattern would otherwise
# claim it too. See `classify()`'s `_sort_key` for how `order` breaks a
# same-priority tie.
_HOSTED_CLUSTER_HYPERSHIFT_PATTERN = r"^ocp4-hypershift"
_HOSTED_CLUSTER_HARDWARE_PATTERN = r"^ocp-"
_MCE_PATTERN = "mce"
_UPI_PATTERN = r".*"


def default_system_rules(sites: SiteCatalog) -> list[ClassificationRule]:
    """
    The four unscoped SYSTEM_DEFAULT rules, as ready-to-persist rules.

    Covers this estate's real hostname conventions: two prefix shapes of
    hosted cluster, one substring match for an MCE hub's own nodes, and
    UPI as the unconditional catch-all for everything else. Order matters
    here — see the comment above `_UPI_PATTERN`.

    All four are `system=True` (locked to enabled-only edits after
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

    Args:
        sites (SiteCatalog): The configured sites. Kept for signature
            stability with existing callers, but unused: since 2026-09-08
            every default pattern is a plain prefix/substring match with
            no site token to interpolate.

    Returns:
        list[ClassificationRule]: The four seeded rules.
    """
    now = utcnow()
    return [
        ClassificationRule(
            id=new_id("classification_rule"),
            name="system-default-hypershift-hosted-cluster",
            description='Hosted control planes: any name starting with "ocp4-hypershift".',
            enabled=True,
            system=True,
            installation_type=InstallationType.HOSTED_CLUSTER,
            scope=RuleScope(),
            field="name",
            pattern=_HOSTED_CLUSTER_HYPERSHIFT_PATTERN,
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
                'any name starting with "ocp-".'
            ),
            enabled=True,
            system=True,
            installation_type=InstallationType.HOSTED_CLUSTER,
            scope=RuleScope(),
            field="name",
            pattern=_HOSTED_CLUSTER_HARDWARE_PATTERN,
            source="SYSTEM_DEFAULT",
            priority=100,
            order=1,
            created_at=now,
            updated_at=now,
        ),
        ClassificationRule(
            id=new_id("classification_rule"),
            name="system-default-mce",
            description=(
                'An MCE hub\'s own nodes: any name containing "mce", checked '
                "before the UPI catch-all below since an MCE hub is itself "
                "UPI-installed and would otherwise be indistinguishable "
                "from it by name alone."
            ),
            enabled=True,
            system=True,
            installation_type=InstallationType.MCE,
            scope=RuleScope(),
            field="name",
            pattern=_MCE_PATTERN,
            source="SYSTEM_DEFAULT",
            priority=100,
            order=2,
            created_at=now,
            updated_at=now,
        ),
        ClassificationRule(
            id=new_id("classification_rule"),
            name="system-default-upi",
            description=(
                "User-provisioned infrastructure: everything not already "
                "claimed by a hosted-cluster or MCE rule above."
            ),
            enabled=True,
            system=True,
            installation_type=InstallationType.UPI,
            scope=RuleScope(),
            field="name",
            pattern=_UPI_PATTERN,
            source="SYSTEM_DEFAULT",
            priority=100,
            order=3,
            created_at=now,
            updated_at=now,
        ),
    ]
