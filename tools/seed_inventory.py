"""CLI: seed MongoDB with deterministic fake inventory data.

Usage:
    uv run python -m tools.seed_inventory --count 1000 --seed 42

Runs the exact same ingestion pipeline (`app.application.services.ingest.
IngestService`) a real collector would go through — this is a seeding
convenience, not a shortcut that writes `Server` documents directly.
"""

from __future__ import annotations

import argparse
import asyncio

import structlog

from app.application.services.ingest import IngestService
from app.config import get_settings
from app.infrastructure.logging import configure_logging
from app.infrastructure.mongodb import MongoClientHolder
from app.infrastructure.mongodb.indexes import ensure_indexes
from app.infrastructure.mongodb.manager_repository import MongoManagerRepository
from app.infrastructure.mongodb.server_repository import MongoServerRepository
from app.infrastructure.mongodb.site_repository import MongoSiteRepository
from app.infrastructure.providers.fake.generator import list_managers, list_sites
from app.infrastructure.providers.fake.provider import FakeProvider

logger = structlog.get_logger(__name__)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Seed the inventory database with deterministic fake data."
    )
    parser.add_argument(
        "--count", type=int, default=1000, help="Number of fake servers to generate."
    )
    parser.add_argument("--seed", type=int, default=42, help="Random seed for reproducible output.")
    return parser.parse_args(argv)


async def _run(*, count: int, seed: int) -> None:
    settings = get_settings()
    configure_logging(
        level=settings.log_level,
        service_name=settings.service_name,
        environment=settings.environment,
    )

    mongo = MongoClientHolder(settings)
    await mongo.connect()
    try:
        await ensure_indexes(mongo.db)

        ingest_service = IngestService(
            server_repo=MongoServerRepository(mongo, cursor_secret=settings.cursor_secret),
            site_repo=MongoSiteRepository(mongo),
            manager_repo=MongoManagerRepository(mongo),
        )
        provider = FakeProvider(seed=seed, count=count)

        summary = await ingest_service.ingest(
            provider, sites=list_sites(), managers=list_managers()
        )

        logger.info(
            "seed.completed",
            fetched=summary.fetched,
            created=summary.created,
            updated=summary.updated,
            errors=summary.errors,
        )
        print(  # CLI output, distinct from the structured log line above
            f"fetched={summary.fetched} created={summary.created} "
            f"updated={summary.updated} errors={summary.errors}"
        )
    finally:
        await mongo.close()


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    asyncio.run(_run(count=args.count, seed=args.seed))


if __name__ == "__main__":
    main()
