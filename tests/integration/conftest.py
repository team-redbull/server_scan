"""Shared fixtures for tests against the live dev Mongo+Redis stack
(`scripts/dev-up.sh`). Every fixture here skips cleanly — never hangs —
when its backing service isn't reachable, so `pytest tests/integration`
degrades to a clear skip list instead of a timeout when the dev stack
isn't running.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from pymongo.errors import PyMongoError

from app.config import get_settings
from app.infrastructure.mongodb import MongoClientHolder
from app.infrastructure.mongodb.indexes import ensure_indexes
from app.infrastructure.redis import RedisClientHolder

_TEST_COLLECTIONS = (
    "servers",
    "sites",
    "managers",
    "classification_rules",
    "health_policies",
    "audit_events",
)


@pytest.fixture
async def mongo_holder() -> AsyncIterator[MongoClientHolder]:
    settings = get_settings()
    holder = MongoClientHolder(settings)
    try:
        await holder.connect()
    except PyMongoError as exc:
        pytest.skip(f"MongoDB not reachable at {settings.mongo_uri}: {exc}")

    await ensure_indexes(holder.db)
    for name in _TEST_COLLECTIONS:
        await holder.db[name].delete_many({})

    try:
        yield holder
    finally:
        for name in _TEST_COLLECTIONS:
            await holder.db[name].delete_many({})
        await holder.close()


@pytest.fixture
async def redis_holder() -> AsyncIterator[RedisClientHolder]:
    settings = get_settings()
    holder = RedisClientHolder(settings)
    await holder.connect()  # never raises; degrades internally
    if not await holder.ping():
        await holder.close()
        pytest.skip(f"Redis not reachable at {settings.redis_uri}")
    try:
        yield holder
    finally:
        await holder.close()
