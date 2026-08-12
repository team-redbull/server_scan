"""Cache-aside `CacheClient`, wrapping the Redis connection pool.

Every method here catches every failure mode Redis can produce
(`redis.exceptions.RedisError` and its subclasses, plus a socket-level
`TimeoutError`) and degrades to a no-op instead of raising: `get()` returns
`None` (indistinguishable from a cache miss to the caller, which is
exactly the point — the caller falls through to MongoDB either way) and
`set()`/`delete()` silently do nothing. This is what makes the "degrade to
Mongo on Redis failure" contract described in
`app.infrastructure.redis.client` actually hold at every call site,
without every route handler needing its own try/except around a cache
call.

JSON (not msgpack) is deliberate for Phase 1: msgpack isn't a project
dependency yet and adding one for a serialization format that's purely an
internal implementation detail isn't worth it until there's a measured
reason to.
"""

from __future__ import annotations

import json
from typing import Any

import structlog

from app.infrastructure.redis.client import RedisClientHolder
from app.observability.metrics import cache_operations_total
from redis.exceptions import RedisError

logger = structlog.get_logger(__name__)

# TTLs are set per resource shape, not from `Settings.cache_default_ttl_seconds`:
# list pages churn much faster than a single server document (any write to
# any server in the result set makes a cached page stale) and are cheaper
# to recompute, so they get a much shorter TTL than the low-churn,
# per-document server cache.
SERVER_DETAIL_TTL_SECONDS = 60
LIST_PAGE_TTL_SECONDS = 15

# `redis-py`'s async client raises the stdlib `TimeoutError` (not a
# `RedisError` subclass) for socket-level timeouts; `asyncio.TimeoutError`
# has been the same class as the builtin since Python 3.11, so listing it
# separately would be a duplicate-exception lint error, not extra coverage.
_CACHE_EXCEPTIONS = (RedisError, TimeoutError)


class CacheClient:
    """Cache-aside wrapper. Never raises — every method degrades to a
    no-op/`None` on any Redis failure so callers never need their own
    try/except around a cache call.
    """

    def __init__(self, redis: RedisClientHolder) -> None:
        self._redis = redis

    async def get(self, key: str) -> Any | None:
        try:
            raw = await self._redis.client.get(key)
        except _CACHE_EXCEPTIONS as exc:
            logger.warning("cache.get_failed", key=key, error=str(exc))
            cache_operations_total.labels(operation="get", outcome="error").inc()
            return None

        if raw is None:
            cache_operations_total.labels(operation="get", outcome="miss").inc()
            return None

        try:
            value = json.loads(raw)
        except (TypeError, ValueError) as exc:
            # A malformed payload (e.g. a truncated write, or a format
            # left over from a previous namespace version) is treated the
            # same as a miss/error, never surfaced to the caller as data.
            logger.warning("cache.decode_failed", key=key, error=str(exc))
            cache_operations_total.labels(operation="get", outcome="error").inc()
            return None

        cache_operations_total.labels(operation="get", outcome="hit").inc()
        return value

    async def set(self, key: str, value: object, *, ttl_seconds: int) -> None:
        try:
            payload = json.dumps(value, default=str)
        except TypeError as exc:
            # Programmer error (a non-JSON-serializable value was passed)
            # — log it, but still never raise out of the cache layer.
            logger.warning("cache.encode_failed", key=key, error=str(exc))
            cache_operations_total.labels(operation="set", outcome="error").inc()
            return

        try:
            await self._redis.client.set(key, payload, ex=ttl_seconds)
        except _CACHE_EXCEPTIONS as exc:
            logger.warning("cache.set_failed", key=key, error=str(exc))
            cache_operations_total.labels(operation="set", outcome="error").inc()
            return

        cache_operations_total.labels(operation="set", outcome="success").inc()

    async def delete(self, key: str) -> None:
        try:
            await self._redis.client.delete(key)
        except _CACHE_EXCEPTIONS as exc:
            logger.warning("cache.delete_failed", key=key, error=str(exc))
            cache_operations_total.labels(operation="delete", outcome="error").inc()
            return

        cache_operations_total.labels(operation="delete", outcome="success").inc()
