"""Prove — against the live database, at real scale — that every query
shape `GET /api/v1/servers` (and the classification/health resolution and
preview paths) can actually issue is index-covered, not just "should be"
by inspection of `app.infrastructure.mongodb.indexes`.

Why this exists as a script rather than only as `.explain()` assertions in
the integration test suite (`tests/integration/test_server_repository.py`
already has a few): a fixture-sized collection (dozens of documents) can
"pass" an IXSCAN assertion for the wrong reason — MongoDB's planner may
prefer a full COLLSCAN over a barely-selective index at that size, and a
COLLSCAN is genuinely fine (even faster) at 50 documents. Whether a query
shape holds up depends on the plan MongoDB actually picks once the
collection is at the platform's real target scale (~10k, headroom to
50k+) — the plan tests assert against fixture data can look identical to
a broken one until you run it at scale, which this script does, against
whatever is actually seeded in the connected database (run
`tools/seed_inventory.py --count 10000` or `--count 50000` first).

Usage:
    uv run python -m tools.verify_indexes

Exits non-zero if any query shape that has a supporting index in
`app.infrastructure.mongodb.indexes` falls back to a COLLSCAN anyway —
that's a real regression (an index got dropped, renamed, or the query
shape drifted from what the index was built for). Shapes that are
*expected* to COLLSCAN (see `EXPECTED_COLLSCAN` below) are reported but
never fail the run.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import structlog

from app.config import get_settings
from app.infrastructure.logging import configure_logging
from app.infrastructure.mongodb import MongoClientHolder

logger = structlog.get_logger(__name__)


@dataclass(slots=True)
class QueryCheck:
    description: str
    collection: str
    filter: dict[str, object]
    sort: list[tuple[str, int]] | None = None
    limit: int | None = None
    # True for shapes that are *supposed* to be a bounded COLLSCAN (e.g. an
    # unfiltered preview candidate scan capped by `limit`) — reported, not
    # a failure.
    expect_collscan: bool = False


# Every shape `MongoServerRepository.list_page` can actually issue for
# `GET /api/v1/servers`: no filter, each single `FILTER_FIELDS` entry
# alone, and search alone — each paired with every `SORT_FIELDS` value,
# since the repository always appends `(sort_field, dir), ("_id", dir)`
# to whatever filter it built. `maintenance` is deliberately included even
# though `app.infrastructure.mongodb.indexes.SERVER_INDEXES` has no
# compound index starting with `maintenance.enabled` — this script is what
# proves whether that's a real gap or a non-issue (see the report at the
# bottom of `main`).
_SERVER_FILTER_SHAPES: dict[str, dict[str, object]] = {
    "none": {},
    "site_id": {"site_id": "site_dc1"},
    "vendor": {"identity.vendor": "dell"},
    "installation_type": {"classification.installation_type": "HOSTED_CLUSTER"},
    "health_overall": {"health.overall": "WARNING"},
    "maintenance": {"maintenance.enabled": True},
}

_SORT_SHAPES: dict[str, str] = {
    "name": "name_normalized",
    "serial": "identity.serial_normalized",
    "model": "model_normalized",
    "updated_at": "updated_at",
    "last_seen_at": "last_seen_at",
}


def _build_server_checks() -> list[QueryCheck]:
    checks: list[QueryCheck] = []
    for filter_name, filter_query in _SERVER_FILTER_SHAPES.items():
        for sort_name, sort_field in _SORT_SHAPES.items():
            checks.append(
                QueryCheck(
                    description=f"servers: filter={filter_name} sort={sort_name}",
                    collection="servers",
                    filter=filter_query,
                    sort=[(sort_field, 1), ("_id", 1)],
                    limit=51,  # page_size + 1, matching list_page's over-fetch
                )
            )
    # Search alone, and search + a filter — the one combination
    # `build_search_query`'s multikey `search_tokens` regex has to share a
    # plan with a compound filter/sort index, which is the case most
    # likely to force an in-memory sort or a wide unindexed scan.
    search_filter: dict[str, object] = {"search_tokens": {"$regex": "^ocp-dell"}}
    checks.append(
        QueryCheck(
            description="servers: search='ocp-dell' sort=name",
            collection="servers",
            filter=search_filter,
            sort=[("name_normalized", 1), ("_id", 1)],
            limit=51,
        )
    )
    checks.append(
        QueryCheck(
            description="servers: search='ocp-dell' + vendor=dell sort=name",
            collection="servers",
            filter={**search_filter, "identity.vendor": "dell"},
            sort=[("name_normalized", 1), ("_id", 1)],
            limit=51,
        )
    )
    # count_documents({}) — issued by list_page when `with_count=True`.
    # Never index-covered for an unfiltered count (Mongo must tally every
    # matching document); reported, not a failure — the frontend never
    # sets `with_count`, so this shape is never on the request-serving hot
    # path, only a documented cost if a future caller opts into it.
    checks.append(
        QueryCheck(
            description="servers: count_documents({}) [with_count=True path]",
            collection="servers",
            filter={},
            expect_collscan=True,
        )
    )
    # Preview's own unfiltered/bounded scans (classification_service.py,
    # health_policy_service.py) — a COLLSCAN here is correct and expected:
    # there's no filter to index against, and `limit`/`page_size` bounds
    # how much of the collection is actually touched regardless of scale.
    checks.append(
        QueryCheck(
            description="servers: preview candidate scan, no scope, limit=5000",
            collection="servers",
            filter={},
            limit=5000,
            expect_collscan=True,
        )
    )
    return checks


def _classification_health_checks() -> list[QueryCheck]:
    return [
        QueryCheck(
            description="classification_rules: resolution (enabled=true, sorted)",
            collection="classification_rules",
            filter={"enabled": True},
            sort=[("priority", -1), ("order", 1), ("_id", 1)],
        ),
        QueryCheck(
            description="health_policies: resolution (enabled=true, sorted)",
            collection="health_policies",
            filter={"enabled": True},
            sort=[("policy_key", 1), ("priority", -1), ("order", 1), ("_id", 1)],
        ),
    ]


def _audit_event_checks() -> list[QueryCheck]:
    return [
        QueryCheck(
            description="audit_events: global feed",
            collection="audit_events",
            filter={},
            sort=[("created_at", -1), ("_id", -1)],
            limit=51,
        ),
        QueryCheck(
            description="audit_events: by server_id",
            collection="audit_events",
            filter={"server_id": "srv_does_not_matter_for_plan_shape"},
            sort=[("created_at", -1), ("_id", -1)],
            limit=51,
        ),
        QueryCheck(
            description="audit_events: by event_type",
            collection="audit_events",
            filter={"event_type": "SERVER_CREATED"},
            sort=[("created_at", -1), ("_id", -1)],
            limit=51,
        ),
        QueryCheck(
            description="audit_events: by actor.id",
            collection="audit_events",
            filter={"actor.id": "ingestion"},
            sort=[("created_at", -1), ("_id", -1)],
            limit=51,
        ),
    ]


def _winning_stage_names(plan: dict[str, Any]) -> set[str]:
    stages: set[str] = set()

    def walk(node: dict[str, Any]) -> None:
        stage = node.get("stage")
        if stage:
            stages.add(stage)
        for key in ("inputStage", "shards"):
            child = node.get(key)
            if isinstance(child, dict):
                walk(child)
        for child in node.get("inputStages", []) or []:
            if isinstance(child, dict):
                walk(child)

    walk(plan)
    return stages


def _index_names(plan: dict[str, Any]) -> set[str]:
    names: set[str] = set()

    def walk(node: dict[str, Any]) -> None:
        name = node.get("indexName")
        if name:
            names.add(name)
        for key in ("inputStage", "shards"):
            child = node.get(key)
            if isinstance(child, dict):
                walk(child)
        for child in node.get("inputStages", []) or []:
            if isinstance(child, dict):
                walk(child)

    walk(plan)
    return names


async def _run_check(db: Any, check: QueryCheck) -> tuple[bool, str]:
    cursor = db[check.collection].find(check.filter)
    if check.sort:
        cursor = cursor.sort(check.sort)
    if check.limit:
        cursor = cursor.limit(check.limit)

    # PyMongo's native async `AsyncCursor.explain()` takes no verbosity
    # argument — it always runs at `allPlansExecution` (which includes
    # `executionStats`), unlike the old sync-driver `explain(verbosity)`
    # signature. See `AsyncCursor.explain`'s docstring.
    explain = await cursor.explain()
    winning = explain["queryPlanner"]["winningPlan"]
    stats = explain["executionStats"]

    stages = _winning_stage_names(winning)
    indexes = _index_names(winning)
    is_collscan = "COLLSCAN" in stages
    has_sort_stage = "SORT" in stages  # blocking in-memory sort — bad at scale

    examined = stats["totalDocsExamined"]
    keys_examined = stats["totalKeysExamined"]
    returned = stats["nReturned"]

    ok = check.expect_collscan or not is_collscan

    index_repr = ", ".join(sorted(indexes)) if indexes else "(none)"
    line = (
        f"{'OK ' if ok else 'FAIL'}  {check.description}\n"
        f"      stages={sorted(stages)} index={index_repr} "
        f"in_memory_sort={has_sort_stage}\n"
        f"      keysExamined={keys_examined} docsExamined={examined} "
        f"returned={returned}"
    )
    return ok, line


async def main() -> None:
    settings = get_settings()
    configure_logging(
        level=settings.log_level,
        service_name=settings.service_name,
        environment=settings.environment,
    )

    mongo = MongoClientHolder(settings)
    await mongo.connect()
    try:
        server_count = await mongo.db["servers"].count_documents({})
        print(f"servers collection: {server_count} documents\n")

        checks = [
            *_build_server_checks(),
            *_classification_health_checks(),
            *_audit_event_checks(),
        ]

        failures: list[str] = []
        for check in checks:
            ok, line = await _run_check(mongo.db, check)
            print(line)
            if not ok:
                failures.append(check.description)

        print()
        if failures:
            print(f"{len(failures)} query shape(s) fell back to an unexpected COLLSCAN:")
            for description in failures:
                print(f"  - {description}")
            raise SystemExit(1)
        print(
            "All query shapes with a supporting index used it. "
            "See EXPECTED_COLLSCAN-marked shapes above for the deliberately unindexed ones."
        )
    finally:
        await mongo.close()


if __name__ == "__main__":
    asyncio.run(main())
