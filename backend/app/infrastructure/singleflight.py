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
one process only*, via a plain in-memory `dict[str, asyncio.Task]` — no
cross-process/cross-pod coordination (that would need a distributed lock
in Redis, with its own contention/staleness failure modes, which isn't
justified here: a load balancer spreads concurrent requests for the same
resource across replicas, so per-process coalescing already removes most
of the duplicate work, and Phase 1 has no multi-worker deployment yet to
even test cross-process contention against).

**The computation is owned by its own `Task`, never by whichever caller
arrives first.** An earlier version ran `compute()` directly inside the
first caller's own task and had every later caller `await` a bare shared
`Future` it wrote the result into. That has two independent failure
modes, both real and both reachable from one disconnecting HTTP client
(not necessarily via ordinary disconnect — Uvicorn surfaces an ordinary
peer disconnect as an ASGI `http.disconnect` event on `receive()`, not a
task cancellation, so the trigger here is graceful-shutdown expiry, a
request timeout, or any other path that calls `task.cancel()` on a
request handler mid-await):

1. A cancelled *waiter* — one of the later callers — cancels the shared
   `Future` it is bare-`await`ing (`Task.cancel()` cancels whatever the
   task is currently awaiting; for a bare Future, that is the Future
   itself). The *leader* then crashes with `InvalidStateError` calling
   `future.set_result(...)` on a Future that's already cancelled, failing
   every other, uninvolved waiter too.
2. A cancelled *leader* — the caller actually running `compute()` — stores
   its own `CancelledError` onto the shared Future via
   `future.set_exception(...)`, which every waiter then re-raises as their
   own result, even ones `asyncio.shield()` would otherwise have
   protected.

Owning the computation in an independent `Task` (created once, the first
time a key is seen) and having every caller — including what used to be
"the leader" — only ever `await asyncio.shield(task)` removes both: no
caller's own cancellation can reach the task doing the work, because no
caller ever runs that work directly in its own task any more.
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Awaitable, Callable

import structlog

logger = structlog.get_logger(__name__)

_inflight: dict[str, asyncio.Task[object]] = {}


async def coalesce[T](key: str, compute: Callable[[], Awaitable[T]]) -> T:
    """Run `compute()` for `key`, sharing the result with any concurrent
    caller that requests the same `key` while it's in flight. A caller
    that arrives after the computation has already finished (or before
    anyone has started it) runs its own fresh call — this only dedupes
    overlap, it is not a cache.
    """
    task = _inflight.get(key)
    if task is None:
        task = asyncio.get_running_loop().create_task(_run(compute))
        _inflight[key] = task
        # Registered *after* insertion, not via `_run`'s own `finally`:
        # under `asyncio.eager_task_factory` (3.12+, not enabled by this
        # project today but a real risk if it ever is — Task execution
        # starts synchronously inside `create_task()`), `compute()` can
        # finish before `create_task()` even returns, so a `finally`
        # inside `_run` would run *before* the `_inflight[key] = task`
        # line above it, find nothing to clean up, and leave a completed
        # Task cached under this key forever. A done-callback added after
        # insertion is scheduled via `call_soon` even for an
        # already-finished Task, so it still runs, just one tick later —
        # verified against this project's own interpreter.
        task.add_done_callback(functools.partial(_cleanup, key))
    return await asyncio.shield(task)  # ty: ignore[invalid-return-type]


async def _run[T](compute: Callable[[], Awaitable[T]]) -> T:
    return await compute()


def _cleanup(key: str, task: asyncio.Task[object]) -> None:
    """
    Remove `key`'s entry once its task has settled, and surface a
    result nobody was left to observe.

    Args:
        key (str): The `_inflight` key this task was registered under.
        task (asyncio.Task[object]): The task that just finished.
    """
    if _inflight.get(key) is task:
        del _inflight[key]
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        # Every shielded waiter that would otherwise have surfaced this
        # exception was itself cancelled before the task settled — the
        # exception would otherwise only be logged by asyncio's own
        # "exception was never retrieved" warning at GC time, with no
        # context at all. Calling `.exception()` above already marks it
        # retrieved either way, so this is purely additive.
        logger.warning("singleflight.unretrieved_exception", key=key, error=str(exc))


async def drain() -> None:
    """
    Cancel and await every in-flight computation.

    Call from the application lifespan's shutdown, *before* closing the
    Mongo/Redis clients a still-running computation may be using —
    `coalesce()` deliberately detaches a computation from every caller
    that stops waiting on it, precisely so one disconnecting client can't
    fail the others, which means a computation can outlive every request
    that ever cared about it. Left to the ordinary event loop shutdown
    (`asyncio.run()`'s own cancellation of remaining tasks happens *after*
    the lifespan's `finally` has already run), such a computation would be
    mid-query against a client that no longer exists.
    """
    tasks = list(_inflight.values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
