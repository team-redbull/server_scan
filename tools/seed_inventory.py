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

from app.application.services.audit_service import AuditService
from app.application.services.bootstrap import (
    ensure_default_classification_rules,
    ensure_default_health_policies,
)
from app.application.services.classification_service import ClassificationService
from app.application.services.health_policy_service import HealthPolicyService
from app.application.services.ingest import IngestService
from app.config import get_settings
from app.domain.services.health.metrics import build_default_registry
from app.domain.services.regex_engine import RegexModuleEngine
from app.domain.value_objects.gpu_catalog import gpu_catalog
from app.domain.value_objects.site import site_catalog
from app.infrastructure.logging import configure_logging
from app.infrastructure.mongodb import MongoClientHolder
from app.infrastructure.mongodb.audit_event_repository import MongoAuditEventRepository
from app.infrastructure.mongodb.classification_rule_repository import (
    MongoClassificationRuleRepository,
)
from app.infrastructure.mongodb.health_policy_repository import MongoHealthPolicyRepository
from app.infrastructure.mongodb.indexes import ensure_indexes
from app.infrastructure.mongodb.manager_repository import MongoManagerRepository
from app.infrastructure.mongodb.server_repository import MongoServerRepository
from app.infrastructure.mongodb.site_repository import MongoSiteRepository
from app.infrastructure.providers.fake.generator import list_managers, list_sites
from app.infrastructure.providers.fake.provider import fake_providers

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
        # Idempotent — see `ensure_default_*`'s docstring. Seeded here (not
        # just in app startup) so `seed_inventory` also works as a
        # standalone script against a fresh database with no API server
        # ever having run against it.
        rule_repo = MongoClassificationRuleRepository(mongo)
        policy_repo = MongoHealthPolicyRepository(mongo)
        sites = site_catalog(settings.sites)
        await ensure_default_classification_rules(rule_repo, sites)
        await ensure_default_health_policies(policy_repo)

        regex_engine = RegexModuleEngine(
            max_pattern_length=settings.regex_max_pattern_length,
            match_timeout_seconds=settings.regex_match_timeout_seconds,
        )
        ingest_service = IngestService(
            server_repo=MongoServerRepository(mongo, cursor_secret=settings.cursor_secret),
            site_repo=MongoSiteRepository(mongo),
            manager_repo=MongoManagerRepository(mongo),
            sites=sites,
            # Threaded explicitly for the same reason `sites` is: the
            # parameter default is the built-in table with nothing
            # configured over it, so without this `INVENTORY_GPU_MODELS`
            # silently did nothing to seeded data — the one place a local
            # override is easiest to try.
            gpu_catalog=gpu_catalog(settings.gpu_models),
            classification_service=ClassificationService(
                rule_repo=rule_repo, engine=regex_engine, mongo=mongo
            ),
            health_service=HealthPolicyService(
                policy_repo=policy_repo,
                registry=build_default_registry(),
                server_repo=MongoServerRepository(mongo, cursor_secret=settings.cursor_secret),
            ),
            audit=AuditService(repo=MongoAuditEventRepository(mongo)),
        )
        # One pass per collector: `Server.source_provider` is stamped from
        # the provider, so a single pass would label the whole fake fleet
        # with one collector that never found most of it.
        fetched = created = updated = errors = 0
        for provider in fake_providers(seed=seed, count=count, sites=sites):
            summary = await ingest_service.ingest(
                provider, sites=list_sites(sites), managers=list_managers()
            )
            fetched += summary.fetched
            created += summary.created
            updated += summary.updated
            errors += summary.errors

        logger.info(
            "seed.completed",
            fetched=fetched,
            created=created,
            updated=updated,
            errors=errors,
        )
        print(  # CLI output, distinct from the structured log line above
            f"fetched={fetched} created={created} updated={updated} errors={errors}"
        )
    finally:
        await mongo.close()


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    asyncio.run(_run(count=args.count, seed=args.seed))


if __name__ == "__main__":
    main()
