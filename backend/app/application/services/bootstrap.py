"""Idempotent seeding of system-default classification rules and health
policies.

Both `default_system_rules()` and `default_system_policies()` generate a
fresh random id on every call, so re-running them and unconditionally
upserting would collide on each collection's unique `name` index on every
startup after the first. Seeding is therefore keyed on `name`, and an
existing document keeps its id, its stats and its `enabled` flag — the one
field a system rule/policy allows an admin to change.

Classification rules go one step further: a system rule's *definition* is
re-synced from code on startup when it has drifted. A default rule's
pattern is generated from `SiteCode` (see
`app.infrastructure.mongodb.classification_rule_repository`), so renaming
a site changes it — and seed-only-if-missing would have left every
existing deployment matching hostnames for sites that no longer exist,
silently, with nothing in the UI to suggest the rule was stale.
"""

from __future__ import annotations

import structlog

from app.domain.models.classification_rule import ClassificationRule
from app.domain.services.health.health_policy_defaults import default_system_policies
from app.domain.value_objects.site import SiteCatalog
from app.infrastructure.mongodb.classification_rule_repository import (
    MongoClassificationRuleRepository,
    default_system_rules,
)
from app.infrastructure.mongodb.health_policy_repository import MongoHealthPolicyRepository
from app.utils.timeutil import utcnow

logger = structlog.get_logger(__name__)


# What an admin owns on a system rule, and so what a re-sync must carry
# over from the stored document rather than reset from code.
_ADMIN_OWNED_RULE_FIELDS = ("id", "enabled", "stats", "created_at", "created_by")


def _resynced(stored: ClassificationRule, generated: ClassificationRule) -> ClassificationRule:
    """
    The generated rule, wearing the stored one's admin-owned fields.

    Args:
        stored (ClassificationRule): The rule as it exists in MongoDB.
        generated (ClassificationRule): The same rule as code defines it now.

    Returns:
        ClassificationRule: The definition to store, with the id, enabled
            flag, match stats and creation audit preserved.
    """
    return generated.model_copy(
        update={field: getattr(stored, field) for field in _ADMIN_OWNED_RULE_FIELDS}
        | {"revision": stored.revision + 1, "updated_at": utcnow()}
    )


def _definition_of(rule: ClassificationRule) -> dict[str, object]:
    """
    The parts of a rule that code owns, for comparing stored against generated.

    Args:
        rule (ClassificationRule): Any classification rule.

    Returns:
        dict[str, object]: Its definition, without ids, timestamps or the
            fields an admin may change.
    """
    return rule.model_dump(
        mode="json",
        exclude={*_ADMIN_OWNED_RULE_FIELDS, "revision", "updated_at", "updated_by"},
    )


async def ensure_default_classification_rules(
    repo: MongoClassificationRuleRepository, sites: SiteCatalog
) -> int:
    """Seed the system-default rules, and re-sync any whose definition has
    drifted from what code now generates.

    Args:
        repo (MongoClassificationRuleRepository): The rules collection.

    Returns:
        int: How many rules were written — created plus re-synced. 0 on
            every call after the first, until a default's definition
            changes in code.
    """
    written = 0
    for rule in default_system_rules(sites):
        stored = await repo.get_by_name(rule.name)
        if stored is None:
            await repo.upsert(rule)
            written += 1
            continue
        if _definition_of(stored) == _definition_of(rule):
            continue
        await repo.upsert(_resynced(stored, rule))
        written += 1
        logger.info("bootstrap.classification_rule_resynced", name=rule.name)
    if written:
        logger.info("bootstrap.classification_rules_seeded", count=written)
    return written


async def ensure_default_health_policies(repo: MongoHealthPolicyRepository) -> int:
    created = 0
    for policy in default_system_policies():
        if await repo.get_by_name(policy.name) is not None:
            continue
        await repo.upsert(policy)
        created += 1
    if created:
        logger.info("bootstrap.health_policies_seeded", count=created)
    return created
