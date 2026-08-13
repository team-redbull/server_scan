"""Integration tests for `MongoHealthPolicyRepository` against the live
dev MongoDB (see `tests/integration/conftest.py` for the
skip-if-unreachable `mongo_holder` fixture). Covers CRUD round-trip, the
unique `name` index, and — the actual point of the
`enabled_policy_key_priority_order_id` compound index — that the family-
resolution load order the domain evaluator expects
(`enabled`, `policy_key`, `priority DESC`, `order ASC`, `_id ASC`) is an
IXSCAN, never a COLLSCAN/blocking in-memory sort.
"""

from __future__ import annotations

import json

import pytest
from pymongo.errors import DuplicateKeyError

from app.domain.enums import HealthSeverity
from app.domain.models.health_policy import HealthPolicy, PolicyScope
from app.domain.services.health.conditions import Condition
from app.domain.services.health.health_policy_defaults import default_system_policies
from app.infrastructure.mongodb import MongoClientHolder
from app.infrastructure.mongodb.health_policy_repository import MongoHealthPolicyRepository
from app.infrastructure.mongodb.indexes import HEALTH_POLICIES_COLLECTION
from app.utils.ids import new_id
from app.utils.timeutil import utcnow

pytestmark = pytest.mark.integration


def _make_policy(
    name: str,
    *,
    policy_key: str | None = None,
    priority: int = 200,
    order: int = 0,
    enabled: bool = True,
    source: str = "GLOBAL_CUSTOM",
    scope: PolicyScope | None = None,
    category: str = "connectivity",
    severity: HealthSeverity = HealthSeverity.WARNING,
) -> HealthPolicy:
    now = utcnow()
    policy_id = new_id("health_policy")
    return HealthPolicy(
        id=policy_id,
        name=name,
        policy_key=policy_key or policy_id,
        category=category,
        severity=severity,
        condition=Condition(metric="connectivity.fabric_paths_down", operator="GTE", value=1),
        message_template="{down} down",
        scope=scope or PolicyScope(),
        source=source,
        priority=priority,
        order=order,
        enabled=enabled,
        created_at=now,
        updated_at=now,
    )


async def test_insert_and_get_by_id_round_trip(mongo_holder: MongoClientHolder) -> None:
    repo = MongoHealthPolicyRepository(mongo_holder)
    policy = _make_policy("policy-round-trip")

    await repo.upsert(policy)
    fetched = await repo.get_by_id(policy.id)

    assert fetched is not None
    assert fetched.id == policy.id
    assert fetched.name == policy.name
    assert fetched.policy_key == policy.policy_key


async def test_get_by_id_missing_returns_none(mongo_holder: MongoClientHolder) -> None:
    repo = MongoHealthPolicyRepository(mongo_holder)
    assert await repo.get_by_id("hpol_does_not_exist") is None


async def test_get_by_name_round_trip(mongo_holder: MongoClientHolder) -> None:
    repo = MongoHealthPolicyRepository(mongo_holder)
    policy = _make_policy("policy-by-name")
    await repo.upsert(policy)

    fetched = await repo.get_by_name("policy-by-name")
    assert fetched is not None
    assert fetched.id == policy.id

    assert await repo.get_by_name("does-not-exist") is None


async def test_delete_returns_true_when_present_false_when_absent(
    mongo_holder: MongoClientHolder,
) -> None:
    repo = MongoHealthPolicyRepository(mongo_holder)
    policy = _make_policy("policy-to-delete")
    await repo.upsert(policy)

    assert await repo.delete(policy.id) is True
    assert await repo.get_by_id(policy.id) is None
    assert await repo.delete(policy.id) is False


async def test_duplicate_name_raises_duplicate_key_error(mongo_holder: MongoClientHolder) -> None:
    repo = MongoHealthPolicyRepository(mongo_holder)
    first = _make_policy("shared-name")
    second = _make_policy("shared-name")

    await repo.upsert(first)
    with pytest.raises(DuplicateKeyError):
        await repo.upsert(second)


async def test_list_all_enabled_only_excludes_disabled(mongo_holder: MongoClientHolder) -> None:
    repo = MongoHealthPolicyRepository(mongo_holder)
    await repo.upsert(_make_policy("enabled-policy", enabled=True))
    await repo.upsert(_make_policy("disabled-policy", enabled=False))

    policies = await repo.list_all(enabled_only=True)

    assert [p.name for p in policies] == ["enabled-policy"]


async def test_list_all_returns_every_policy_regardless_of_enabled(
    mongo_holder: MongoClientHolder,
) -> None:
    repo = MongoHealthPolicyRepository(mongo_holder)
    await repo.upsert(_make_policy("enabled-policy", enabled=True))
    await repo.upsert(_make_policy("disabled-policy", enabled=False))

    policies = await repo.list_all()

    assert {p.name for p in policies} == {"enabled-policy", "disabled-policy"}


async def test_default_system_policies_round_trip(mongo_holder: MongoClientHolder) -> None:
    repo = MongoHealthPolicyRepository(mongo_holder)
    for policy in default_system_policies():
        await repo.upsert(policy)

    policies = await repo.list_all(enabled_only=True)
    assert len(policies) == 3
    keys = {p.policy_key for p in policies}
    assert keys == {
        "connectivity.fabric_paths_down_warning",
        "connectivity.fabric_paths_down_critical",
        "storage.failed_drive",
    }
    assert all(p.system for p in policies)
    assert all(p.source == "SYSTEM_DEFAULT" for p in policies)


# --- Index assertions ---


async def test_declared_indexes_exist(mongo_holder: MongoClientHolder) -> None:
    repo = MongoHealthPolicyRepository(mongo_holder)
    await repo.upsert(_make_policy("index-probe"))  # ensure collection exists

    cursor = await mongo_holder.db[HEALTH_POLICIES_COLLECTION].list_indexes()
    index_docs = await cursor.to_list(length=None)
    index_names = {doc["name"] for doc in index_docs}

    assert "uniq_name" in index_names
    assert "enabled_policy_key_priority_order_id" in index_names
    assert "policy_key" in index_names
    assert "category" in index_names
    assert "scope_site_id" in index_names


async def test_family_resolution_load_order_uses_index_scan(
    mongo_holder: MongoClientHolder,
) -> None:
    """The whole point of the `enabled_policy_key_priority_order_id`
    compound index: the standard "load all enabled policies pre-sorted
    for family resolution" query — filter `{enabled: true}`, sort
    `(policy_key ASC, priority DESC, order ASC, _id ASC)` — must be an
    IXSCAN, never a COLLSCAN/blocking in-memory SORT stage.
    """
    repo = MongoHealthPolicyRepository(mongo_holder)
    for i in range(20):
        await repo.upsert(
            _make_policy(
                f"policy-{i:03d}",
                policy_key=f"key-{i % 4}",
                priority=200 + i,
                enabled=i % 2 == 0,
            )
        )

    collection = mongo_holder.db[HEALTH_POLICIES_COLLECTION]
    explain = await (
        collection.find({"enabled": True})
        .sort([("policy_key", 1), ("priority", -1), ("order", 1), ("_id", 1)])
        .explain()
    )
    explain_str = json.dumps(explain)

    assert "COLLSCAN" not in explain_str
    assert "IXSCAN" in explain_str
    assert '"stage": "SORT"' not in explain_str
