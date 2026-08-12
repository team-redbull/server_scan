"""Integration tests for `CacheClient`.

Covers both the happy path against the live dev Redis (skips cleanly if
unreachable — see `tests/integration/conftest.py`) and the degrade-on-
failure path, which is the actually load-bearing behavior: every method
must return fast (no hang) and never raise when Redis is unreachable.
"""

from __future__ import annotations

import time

import pytest

from app.config import Settings
from app.infrastructure.redis.cache import CacheClient
from app.infrastructure.redis.client import RedisClientHolder

pytestmark = pytest.mark.integration


async def test_set_then_get_round_trips(redis_holder: RedisClientHolder) -> None:
    cache = CacheClient(redis_holder)
    await cache.set("test:cache:roundtrip", {"hello": "world", "n": 5}, ttl_seconds=30)

    value = await cache.get("test:cache:roundtrip")

    assert value == {"hello": "world", "n": 5}


async def test_get_missing_key_returns_none(redis_holder: RedisClientHolder) -> None:
    cache = CacheClient(redis_holder)
    assert await cache.get("test:cache:definitely-not-set") is None


async def test_delete_removes_key(redis_holder: RedisClientHolder) -> None:
    cache = CacheClient(redis_holder)
    await cache.set("test:cache:to-delete", {"x": 1}, ttl_seconds=30)
    assert await cache.get("test:cache:to-delete") == {"x": 1}

    await cache.delete("test:cache:to-delete")

    assert await cache.get("test:cache:to-delete") is None


async def _unreachable_holder() -> RedisClientHolder:
    settings = Settings(
        redis_uri="redis://localhost:1/0",
        redis_connect_timeout_seconds=0.5,
        redis_socket_timeout_seconds=0.5,
    )
    holder = RedisClientHolder(settings)
    await holder.connect()  # never raises, even though nothing is listening
    return holder


async def test_get_degrades_to_none_when_redis_unreachable() -> None:
    holder = await _unreachable_holder()
    cache = CacheClient(holder)

    start = time.monotonic()
    value = await cache.get("any-key")
    elapsed = time.monotonic() - start

    assert value is None
    assert elapsed < 5.0  # fails fast, does not hang
    await holder.close()


async def test_set_degrades_silently_when_redis_unreachable() -> None:
    holder = await _unreachable_holder()
    cache = CacheClient(holder)

    start = time.monotonic()
    await cache.set("any-key", {"a": 1}, ttl_seconds=30)  # must not raise
    elapsed = time.monotonic() - start

    assert elapsed < 5.0
    await holder.close()


async def test_delete_degrades_silently_when_redis_unreachable() -> None:
    holder = await _unreachable_holder()
    cache = CacheClient(holder)

    start = time.monotonic()
    await cache.delete("any-key")  # must not raise
    elapsed = time.monotonic() - start

    assert elapsed < 5.0
    await holder.close()
