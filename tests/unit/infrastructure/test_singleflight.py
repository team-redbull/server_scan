"""`app.infrastructure.singleflight.coalesce` — in-process request
coalescing, added in the slice 6 performance pass after `tools/loadtest.py`
found a cache-stampede tail-latency problem on `GET /api/v1/servers`.
"""

from __future__ import annotations

import asyncio

import pytest

from app.infrastructure.singleflight import coalesce

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

    async def make_compute(key: str):
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
