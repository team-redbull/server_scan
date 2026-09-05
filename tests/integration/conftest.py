"""Shared fixtures for tests against the live dev Mongo+Redis stack
(`scripts/dev-up.sh`). Every fixture here skips cleanly — never hangs —
when its backing service isn't reachable, so `pytest tests/integration`
degrades to a clear skip list instead of a timeout when the dev stack
isn't running.

The first failure is remembered for the rest of the session. Without
that, "skips cleanly" was true per test but not per run: these fixtures
are function-scoped, so every one of the ~60 Mongo-backed tests paid
`mongo_server_selection_timeout_ms` (5s) to rediscover the same dead
server. One file of 7 skips took 35s and a whole run took minutes of
doing nothing, which reads as a hung suite rather than a stack that is
simply not up.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from pymongo.errors import PyMongoError

from app.config import get_settings
from app.infrastructure.mongodb import MongoClientHolder
from app.infrastructure.mongodb.indexes import ensure_indexes
from app.infrastructure.redis import RedisClientHolder

# Why a plain dict and not a session-scoped fixture: these are async and
# function-scoped, and widening their scope drags the event-loop scope
# with it. One remembered string is the whole feature.
#
# A service that comes up mid-run stays skipped until the next run. That
# is deliberate — a single run reporting some tests skipped-as-unreachable
# and others passed would be harder to read than a uniformly skipped one.
_UNREACHABLE: dict[str, str] = {}

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
    if (cached := _UNREACHABLE.get("mongo")) is not None:
        pytest.skip(cached)

    settings = get_settings()
    holder = MongoClientHolder(settings)
    try:
        await holder.connect()
    except PyMongoError as exc:
        reason = f"MongoDB not reachable at {settings.mongo_uri}: {exc}"
        _UNREACHABLE["mongo"] = reason
        pytest.skip(reason)

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
    if (cached := _UNREACHABLE.get("redis")) is not None:
        pytest.skip(cached)

    settings = get_settings()
    holder = RedisClientHolder(settings)
    await holder.connect()  # never raises; degrades internally
    if not await holder.ping():
        await holder.close()
        reason = f"Redis not reachable at {settings.redis_uri}"
        _UNREACHABLE["redis"] = reason
        pytest.skip(reason)
    try:
        yield holder
    finally:
        await holder.close()
