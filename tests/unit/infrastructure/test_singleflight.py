"""`app.infrastructure.singleflight.coalesce` — in-process request
coalescing, added in the slice 6 performance pass after `tools/loadtest.py`
found a cache-stampede tail-latency problem on `GET /api/v1/servers`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import pytest

from app.infrastructure import singleflight
from app.infrastructure.singleflight import coalesce, drain

pytestmark = pytest.mark.unit


async def test_concurrent_identical_keys_share_one_computation() -> None:
    call_count = 0
    started = asyncio.Event()

    async def compute() -> str:
        nonlocal call_count
        call_count += 1
        started.set()
        await asyncio.sleep(0.05)
        return "result"

    async def waiter() -> str:
        await started.wait()
        return await coalesce("k", compute)

    first = asyncio.create_task(coalesce("k", compute))
    # Give `first` a chance to register itself and start `compute()` before
    # the others arrive, so they observe the in-flight future rather than
    # racing to register their own.
    others = [asyncio.create_task(waiter()) for _ in range(9)]

    results = await asyncio.gather(first, *others)

    assert call_count == 1
    assert results == ["result"] * 10


async def test_distinct_keys_do_not_block_each_other() -> None:
    call_counts: dict[str, int] = {"a": 0, "b": 0}

    async def make_compute(key: str) -> Callable[[], Awaitable[str]]:
        async def compute() -> str:
            call_counts[key] += 1
            await asyncio.sleep(0.01)
            return key

        return compute

    results = await asyncio.gather(
        coalesce("a", await make_compute("a")),
        coalesce("b", await make_compute("b")),
    )

    assert results == ["a", "b"]
    assert call_counts == {"a": 1, "b": 1}


async def test_exception_propagates_to_every_waiter() -> None:
    started = asyncio.Event()

    async def compute() -> str:
        started.set()
        await asyncio.sleep(0.02)
        raise ValueError("boom")

    async def waiter() -> None:
        await started.wait()
        await coalesce("err", compute)

    first = asyncio.create_task(coalesce("err", compute))
    others = [asyncio.create_task(waiter()) for _ in range(3)]

    results = await asyncio.gather(first, *others, return_exceptions=True)

    assert all(isinstance(r, ValueError) for r in results)


async def test_a_later_call_after_completion_runs_fresh() -> None:
    call_count = 0

    async def compute() -> int:
        nonlocal call_count
        call_count += 1
        return call_count

    first = await coalesce("seq", compute)
    second = await coalesce("seq", compute)

    assert first == 1
    assert second == 2


async def test_a_cancelled_waiter_does_not_fail_the_leader() -> None:
    """The bug this module exists to fix: a *later* caller being cancelled
    used to cancel the bare `Future` every caller — including the one
    actually running `compute()` — shared, crashing the leader with
    `InvalidStateError`. Owning `compute()` in its own `Task` and only
    ever `shield()`ing it removes that: `Task.cancel()` on a caller
    cancels whatever *that caller* is awaiting (the outer `shield()`
    Future), never the shared task itself.
    """
    started = asyncio.Event()

    async def compute() -> str:
        started.set()
        await asyncio.sleep(0.05)
        return "result"

    leader = asyncio.create_task(coalesce("waiter-cancel", compute))
    await started.wait()
    waiter = asyncio.create_task(coalesce("waiter-cancel", compute))
    await asyncio.sleep(0)  # let `waiter` register against the same task
    waiter.cancel()

    with pytest.raises(asyncio.CancelledError):
        await waiter

    assert await leader == "result"


async def test_a_cancelled_leader_does_not_fail_the_waiters() -> None:
    """The other half of the same bug: the *first* caller used to run
    `compute()` directly in its own task, so cancelling it stored a
    `CancelledError` onto the shared Future, failing every other,
    uninvolved waiter too — even ones a `shield()`-only fix would
    otherwise protect. `compute()` now runs in a `Task` nobody's own
    cancellation reaches.
    """
    call_count = 0
    started = asyncio.Event()

    async def compute() -> str:
        nonlocal call_count
        call_count += 1
        started.set()
        await asyncio.sleep(0.05)
        return "result"

    first = asyncio.create_task(coalesce("leader-cancel", compute))
    await started.wait()
    second = asyncio.create_task(coalesce("leader-cancel", compute))
    await asyncio.sleep(0)
    first.cancel()

    with pytest.raises(asyncio.CancelledError):
        await first

    assert await second == "result"
    assert call_count == 1


async def test_the_entry_stays_until_the_task_settles_even_if_every_caller_cancels() -> None:
    """Not "a cancelled caller leaves no entry behind" — the computation
    is still running and still owns the key until it actually finishes.
    Removing the entry the moment a caller cancels would let a second,
    unrelated caller start a duplicate computation while the first is
    still in flight.
    """
    started = asyncio.Event()
    release = asyncio.Event()

    async def compute() -> str:
        started.set()
        await release.wait()
        return "result"

    waiter = asyncio.create_task(coalesce("stays", compute))
    await started.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    assert "stays" in singleflight._inflight
    inner_task = singleflight._inflight["stays"]
    release.set()
    await inner_task  # let it actually finish and its done-callback run
    assert "stays" not in singleflight._inflight


async def test_an_unretrieved_exception_is_logged_not_silently_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every waiter cancelling before `compute()` fails must not mean the
    failure vanishes without a trace — that's what asyncio's own bare
    "exception was never retrieved" GC-time warning would otherwise be.

    Asserted via `monkeypatch` on the module's own `logger`, not
    `structlog.testing.capture_logs()`: `structlog`'s
    `cache_logger_on_first_use` (this project's own logging config, see
    `app.infrastructure.logging.config.configure_logging`) freezes a
    logger's processors the *first* time it actually logs — and a prior,
    unrelated test elsewhere in the suite booting the real app can be that
    first use, against a `structlog.configure()` call that has since been
    replaced by a later one. `capture_logs()` can then no longer reach an
    already-frozen logger. Spying on the call directly sidesteps that
    entirely and is a fair test of "was this warning emitted" regardless.
    """
    calls: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        singleflight.logger, "warning", lambda event, **kw: calls.append((event, kw))
    )
    started = asyncio.Event()

    async def compute() -> str:
        started.set()
        await asyncio.sleep(0.02)
        raise ValueError("boom")

    waiter = asyncio.create_task(coalesce("logged", compute))
    await started.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    await asyncio.sleep(0.05)  # let compute() finish and the done-callback run

    assert calls == [("singleflight.unretrieved_exception", {"key": "logged", "error": "boom"})]


async def test_eager_task_execution_still_cleans_up_the_entry() -> None:
    """Reproduces the race a `finally` inside the task itself would hit
    under `asyncio.eager_task_factory` (3.12+, not enabled by this
    project today, but a real risk if it ever is): eager execution can
    run `compute()` to completion *inside* `create_task()`, before the
    caller gets to register the task in `_inflight` at all. A `finally`
    clause inside the task's own coroutine would then find nothing to
    clean up and exit silently, leaving the already-finished task cached
    under this key forever. Registering cleanup via `add_done_callback`
    *after* insertion survives this because a callback added to an
    already-done Task is still scheduled, just one tick later.
    """
    loop = asyncio.get_running_loop()
    previous_factory = loop.get_task_factory()
    loop.set_task_factory(asyncio.eager_task_factory)
    try:

        async def instant_compute() -> str:
            return "eager-result"  # never awaits/suspends

        result = await coalesce("eager", instant_compute)
        assert result == "eager-result"
        await asyncio.sleep(0)  # let the done-callback's call_soon fire
        assert "eager" not in singleflight._inflight
    finally:
        loop.set_task_factory(previous_factory)


async def test_drain_cancels_every_in_flight_computation() -> None:
    """The lifespan-shutdown hook: a computation nobody is waiting on any
    more must not be left running against clients `drain()`'s caller is
    about to close.
    """
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def compute() -> str:
        started.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return "result"

    task = asyncio.create_task(coalesce("drain-me", compute))
    await started.wait()

    await drain()

    assert cancelled.is_set()
    assert "drain-me" not in singleflight._inflight
    with pytest.raises(asyncio.CancelledError):
        await task
