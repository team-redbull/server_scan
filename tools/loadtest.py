"""A minimal concurrent latency benchmark against a running API instance.

Why a hand-rolled `asyncio` + `httpx` script rather than k6 or Locust: this
is an air-gapped, uv-managed, pure-Python backend with `httpx` already a
project dependency (used in the test suite) — adding k6 means shipping and
mirroring a separate Go binary into the air-gapped registry for a single
benchmark script, and Locust means a second, heavier Python dependency
(its own web UI, distributed-worker protocol) for a need this project
doesn't have: one operator, one machine, one report. A ~150-line asyncio
script that reuses the same `httpx.AsyncClient` the rest of the codebase
already depends on is the smaller, more inspectable choice for what's
actually being measured here — steady-state p50/p95/p99 latency per
endpoint shape under N concurrent callers, at whatever scale the target
database is currently seeded to.

Usage:
    uv run python -m tools.loadtest --base-url http://localhost:8080 \
        --concurrency 20 --requests-per-scenario 200

Prints one line per scenario: p50/p95/p99/max latency (ms) and
requests/sec. Exits non-zero if any request returned a non-2xx status —
a latency number from a run that included real errors isn't trustworthy.
"""

from __future__ import annotations

import argparse
import asyncio
import time
from dataclasses import dataclass, field

import httpx


@dataclass(slots=True)
class Scenario:
    name: str
    method: str
    path: str
    params: dict[str, str] = field(default_factory=dict)


def _scenarios() -> list[Scenario]:
    return [
        Scenario("list: no filter, default sort", "GET", "/api/v1/servers"),
        Scenario(
            "list: filter=vendor sort=name",
            "GET",
            "/api/v1/servers",
            {"vendor": "dell", "sort": "name"},
        ),
        Scenario(
            "list: filter=installation_type sort=name",
            "GET",
            "/api/v1/servers",
            {"installation_type": "HOSTED_CLUSTER", "sort": "name"},
        ),
        Scenario(
            "list: filter=health_overall sort=name",
            "GET",
            "/api/v1/servers",
            {"health_overall": "WARNING", "sort": "name"},
        ),
        Scenario(
            "list: search (selective)",
            "GET",
            "/api/v1/servers",
            {"search": "ocp-dell-worker-0001"},
        ),
        Scenario(
            "list: search (low-selectivity, ~1/4 of fleet)",
            "GET",
            "/api/v1/servers",
            {"search": "ocp-dell"},
        ),
        Scenario("list: sort=last_seen_at", "GET", "/api/v1/servers", {"sort": "last_seen_at"}),
        Scenario("list: page_size=100", "GET", "/api/v1/servers", {"page_size": "100"}),
    ]


async def _run_scenario(
    client: httpx.AsyncClient, scenario: Scenario, *, concurrency: int, total_requests: int
) -> tuple[str, list[float], int, float]:
    latencies: list[float] = []
    errors = 0
    semaphore = asyncio.Semaphore(concurrency)

    async def one_request() -> None:
        nonlocal errors
        async with semaphore:
            start = time.perf_counter()
            response = await client.get(scenario.path, params=scenario.params)
            elapsed_ms = (time.perf_counter() - start) * 1000
            latencies.append(elapsed_ms)
            if response.status_code >= 300:
                errors += 1

    batch_start = time.perf_counter()
    await asyncio.gather(*(one_request() for _ in range(total_requests)))
    wall_seconds = time.perf_counter() - batch_start
    return scenario.name, latencies, errors, wall_seconds


def _percentile(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    index = min(len(sorted_values) - 1, int(len(sorted_values) * pct))
    return sorted_values[index]


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8080")
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--requests-per-scenario", type=int, default=200)
    args = parser.parse_args()

    total_errors = 0
    async with httpx.AsyncClient(base_url=args.base_url, timeout=30.0) as client:
        health = await client.get("/health/ready")
        health.raise_for_status()

        server_count = (await client.get("/api/v1/servers", params={"page_size": "1"})).json()

        print(f"target: {args.base_url}  concurrency={args.concurrency} "
              f"requests_per_scenario={args.requests_per_scenario}")
        print(f"servers collection sample check: {len(server_count.get('items', []))} item(s) "
              "returned for page_size=1\n")

        for scenario in _scenarios():
            name, latencies, errors, wall_seconds = await _run_scenario(
                client,
                scenario,
                concurrency=args.concurrency,
                total_requests=args.requests_per_scenario,
            )
            total_errors += errors
            latencies.sort()
            p50 = _percentile(latencies, 0.50)
            p95 = _percentile(latencies, 0.95)
            p99 = _percentile(latencies, 0.99)
            worst = latencies[-1] if latencies else 0.0
            rps = len(latencies) / wall_seconds if wall_seconds > 0 else 0.0
            error_flag = f"  ERRORS={errors}" if errors else ""
            print(
                f"{name:55s} p50={p50:7.1f}ms p95={p95:7.1f}ms "
                f"p99={p99:7.1f}ms max={worst:7.1f}ms  ~{rps:6.0f} req/s{error_flag}"
            )

    if total_errors:
        raise SystemExit(f"\n{total_errors} request(s) returned a non-2xx status.")


if __name__ == "__main__":
    asyncio.run(main())
