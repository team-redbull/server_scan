"""Shared "is the dev stack up" memo for every test directory that talks to
the live dev Mongo/Redis (`scripts/dev-up.sh`).

`tests/integration/conftest.py` originally owned this logic alone — one
memoized "unreachable" reason per service, remembered for the rest of the
session so ~60 function-scoped fixtures don't each pay
`mongo_server_selection_timeout_ms` to rediscover the same dead server (see
that module's own docstring for the measured before/after). `tests/api/`
needs the identical skip-cleanly behaviour: its fixtures build a real app
and drive `app.router.lifespan_context(app)`, whose own `mongo.connect()`
raises straight out of that context manager on a dead Mongo — an error, not
a skip, which is exactly the "hung suite" symptom this module exists to
prevent. Lifted out so both directories share one memo instead of two that
could drift.

Not a filename pytest would collect as a test module — no `test_` prefix.
"""

from __future__ import annotations

import pytest
from pymongo.errors import PyMongoError

from app.config import Settings
from app.infrastructure.mongodb import MongoClientHolder
from app.infrastructure.redis import RedisClientHolder

# A service that comes up mid-run stays skipped until the next run. That is
# deliberate — a single run reporting some tests skipped-as-unreachable and
# others passed would be harder to read than a uniformly skipped one.
_UNREACHABLE: dict[str, str] = {}


async def connect_mongo_or_skip(settings: Settings) -> MongoClientHolder:
    """
    Connect to Mongo, or skip the current test if it can't be reached.

    Memoizes an unreachable verdict for the rest of the session, so only
    the first caller pays the connection timeout.

    Args:
        settings (Settings): Resolved settings naming `mongo_uri`.

    Returns:
        MongoClientHolder: A connected holder — the caller owns closing it.

    Raises:
        pytest.skip.Exception: If Mongo could not be reached, this call or
            a previous one.
    """
    if (cached := _UNREACHABLE.get("mongo")) is not None:
        pytest.skip(cached)

    holder = MongoClientHolder(settings)
    try:
        await holder.connect()
    except (PyMongoError, OSError) as exc:
        # OSError alongside PyMongoError: a malformed URI scheme raises
        # outside pymongo's own hierarchy, and would otherwise propagate as
        # an error instead of memoizing — reintroducing the exact symptom
        # this module exists to prevent for that one case.
        reason = f"MongoDB not reachable at {settings.mongo_uri}: {exc}"
        _UNREACHABLE["mongo"] = reason
        pytest.skip(reason)
    return holder


async def connect_redis_or_skip(settings: Settings) -> RedisClientHolder:
    """
    Connect to Redis, or skip the current test if it can't be reached.

    Memoizes an unreachable verdict for the rest of the session, so only
    the first caller pays the connection attempt.

    Args:
        settings (Settings): Resolved settings naming `redis_uri`.

    Returns:
        RedisClientHolder: A connected, pinged holder — the caller owns
            closing it.

    Raises:
        pytest.skip.Exception: If Redis could not be reached, this call or
            a previous one.
    """
    if (cached := _UNREACHABLE.get("redis")) is not None:
        pytest.skip(cached)

    holder = RedisClientHolder(settings)
    await holder.connect()  # never raises; degrades internally
    if not await holder.ping():
        await holder.close()
        reason = f"Redis not reachable at {settings.redis_uri}"
        _UNREACHABLE["redis"] = reason
        pytest.skip(reason)
    return holder
