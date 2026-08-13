"""Idempotent seeding of system-default classification rules and health
policies.

Both `default_system_rules()` and `default_system_policies()` generate a
fresh random id on every call, so re-running them and unconditionally
upserting would collide on each collection's unique `name` index on every
startup after the first. "Seed only if missing, by name" is the correct
idempotent behavior here — and it has a second benefit beyond avoiding the
collision: a rule/policy that already exists is left alone entirely, not
partially reset, so an admin's own edit to a system default's `enabled`
flag (the one field a system rule/policy allows changing) survives every
subsequent app restart instead of being silently re-armed.
"""

from __future__ import annotations

import structlog

from app.domain.services.health.health_policy_defaults import default_system_policies
from app.infrastructure.mongodb.classification_rule_repository import (
    MongoClassificationRuleRepository,
    default_system_rules,
)
from app.infrastructure.mongodb.health_policy_repository import MongoHealthPolicyRepository

logger = structlog.get_logger(__name__)


async def ensure_default_classification_rules(repo: MongoClassificationRuleRepository) -> int:
    """Returns the number of rules actually created (0 on every call after
    the first, once seeded).
    """
    created = 0
    for rule in default_system_rules():
        if await repo.get_by_name(rule.name) is not None:
            continue
        await repo.upsert(rule)
        created += 1
    if created:
        logger.info("bootstrap.classification_rules_seeded", count=created)
    return created


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
