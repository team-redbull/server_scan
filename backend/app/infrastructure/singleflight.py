"""In-process request coalescing ("single-flight") for cache-miss stampedes.

Found via `tools/loadtest.py` against a real 50k-server dataset: a
moderately-selective `GET /api/v1/servers?search=...` (matching roughly a
quarter of the fleet — not the common "one specific hostname" case, but a
realistic one, e.g. a saved dashboard filter many browser tabs poll at
once) has p50 latency in the tens of milliseconds but a p95/p99 in the
*seconds* under concurrent identical requests. Root cause: `CacheClient`
is a plain cache-aside (`app.infrastructure.redis.cache`) with no
deduplication — when N identical requests arrive within the same
15-second `LIST_PAGE_TTL_SECONDS` window before the first one has written
its result back, every one of them independently misses the cache and
independently re-runs the same non-trivial Mongo query (a multi-thousand-
document scan-and-regex-filter for a low-selectivity search term), so
Mongo does N times the work instead of 1 and the stragglers queue behind
each other. This is the standard "cache stampede" / "dogpile effect"
failure mode of plain cache-aside under concurrent load; the fix here —
request coalescing keyed by the same cache key — is likewise the standard
mitigation (Go's `singleflight` package and memcached's own documentation
describe the identical pattern).

Scope, deliberately: this coalesces concurrent identical requests *within
one process only*, via a plain in-memory `dict[str, asyncio.Future]` — no
cross-process/cross-pod coordination (that would need a distributed lock
in Redis, with its own contention/staleness failure modes, which isn't
justified here: a load balancer spreads concurrent requests for the same
resource across replicas, so per-process coalescing already removes most
of the duplicate work, and Phase 1 has no multi-worker deployment yet to
even test cross-process contention against).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

_inflight: dict[str, asyncio.Future[object]] = {}


async def coalesce[T](key: str, compute: Callable[[], Awaitable[T]]) -> T:
    """Run `compute()` for `key`, sharing the result with any concurrent
    caller that requests the same `key` while it's in flight. A caller
    that arrives after `compute()` has already finished (or before anyone
    has started it) runs its own fresh call — this only dedupes overlap,
    it is not a cache.
    """
    existing = _inflight.get(key)
    if existing is not None:
        return await existing  # type: ignore[return-value]

    future: asyncio.Future[object] = asyncio.get_running_loop().create_future()
    _inflight[key] = future
    try:
        result = await compute()
    except BaseException as exc:  # propagate to every waiter, then re-raise here
        future.set_exception(exc)
        # Mark the exception "retrieved" from this function's own side too
        # — otherwise, when no concurrent waiter ever awaits `future`,
        # asyncio logs a spurious "exception was never retrieved" warning
        # at GC time even though the real exception is about to propagate
        # via the `raise` below.
        future.exception()
        raise
    else:
        future.set_result(result)
        return result
    finally:
        # Only the caller that registered this exact future clears it —
        # if a slow finally somehow overlapped a new registration for the
        # same key, popping unconditionally could delete someone else's
        # in-flight future instead of this one's.
        if _inflight.get(key) is future:
            del _inflight[key]
