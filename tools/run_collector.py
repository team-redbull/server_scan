"""CLI: run a real vendor collector against every enabled `Manager` of a
given type, ingesting whatever it reports through the exact same
`IngestService` pipeline `tools/seed_inventory.py` exercises with fake
data (classify -> health-evaluate -> audit -> upsert, in one write).

This is what a Kubernetes `CronJob` actually invokes — one CronJob per
manager type (`--manager-type UCS_MANAGER`, etc.), matching how the
platform's own `Manager` documents are already partitioned. A manager
failing (unreachable, bad credentials, an unexpected API response) is
logged and counted but never aborts the run for the *other* managers of
the same type — one flaky UCS domain shouldn't block ingesting the
other nine.

Usage:
    uv run python -m tools.run_collector --manager-type UCS_MANAGER
"""

from __future__ import annotations

import argparse
import asyncio

import structlog

from app.application.services.audit_service import AuditService
from app.application.services.classification_service import ClassificationService
from app.application.services.health_policy_service import HealthPolicyService
from app.application.services.ingest import IngestService, IngestSummary
from app.config import get_settings
from app.domain.enums import ManagerType
from app.domain.models.manager import Manager
from app.domain.ports.credentials import CredentialNotFoundError
from app.domain.ports.provider import ServerInventoryProvider
from app.domain.services.health.metrics import build_default_registry
from app.domain.services.regex_engine import RegexModuleEngine
from app.infrastructure.credentials import FilesystemCredentialResolver
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
from app.infrastructure.providers.ucs_manager.provider import UcsManagerProvider

logger = structlog.get_logger(__name__)

# One entry per manager type this tool actually knows how to collect
# from. UCS_CENTRAL is deliberately absent — it's a domain-discovery
# parent over one or more UCS_MANAGER children (see `Manager`'s own
# docstring), not itself a source of server inventory. Every other
# missing entry is a real gap (OpenManage, Intersight, OneView), not an
# oversight: `_build_provider` raises a clear "not implemented yet" for
# any `manager.type` without an entry here, rather than silently
# skipping that manager.
_PROVIDER_FACTORIES = {
    ManagerType.UCS_MANAGER: UcsManagerProvider,
}


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a real vendor collector against every enabled Manager of one type."
    )
    parser.add_argument(
        "--manager-type",
        required=True,
        choices=sorted(ManagerType.__members__),
        help="Only Manager documents of this type are collected from.",
    )
    return parser.parse_args(argv)


async def _build_provider(
    manager: Manager, *, credential_resolver: FilesystemCredentialResolver, timeout_seconds: float
) -> ServerInventoryProvider:
    factory = _PROVIDER_FACTORIES.get(manager.type)
    if factory is None:
        raise NotImplementedError(
            f"No collector implemented yet for manager type {manager.type!r}."
        )
    if not manager.credential_ref:
        raise CredentialNotFoundError(
            f"Manager {manager.id!r} ({manager.name!r}) has no credential_ref configured."
        )
    credentials = await credential_resolver.resolve(manager.credential_ref)
    return factory(manager=manager, credentials=credentials, timeout_seconds=timeout_seconds)


async def _run_one_manager(
    manager: Manager,
    *,
    ingest_service: IngestService,
    credential_resolver: FilesystemCredentialResolver,
    timeout_seconds: float,
) -> IngestSummary | None:
    try:
        provider = await _build_provider(
            manager, credential_resolver=credential_resolver, timeout_seconds=timeout_seconds
        )
        # No explicit `provider.health_check()` here: `IngestService.
        # ingest()` already calls it as its first step, and a UCS login is
        # ~4 sequential HTTP round trips (auth, then the SDK's own
        # is-this-UCSM / version / domain-name probes), so calling it here
        # too would double that cost per manager and burn a second session
        # against UCS Manager's per-user session cap for nothing — this
        # `except` handles a health-check failure identically either way.
        return await ingest_service.ingest(provider)
    except Exception:
        logger.exception(
            "collector.manager_failed", manager_id=manager.id, manager_name=manager.name
        )
        return None


async def _run(*, manager_type: ManagerType) -> int:
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

        manager_repo = MongoManagerRepository(mongo)
        rule_repo = MongoClassificationRuleRepository(mongo)
        policy_repo = MongoHealthPolicyRepository(mongo)
        regex_engine = RegexModuleEngine(
            max_pattern_length=settings.regex_max_pattern_length,
            match_timeout_seconds=settings.regex_match_timeout_seconds,
        )
        server_repo = MongoServerRepository(mongo, cursor_secret=settings.cursor_secret)
        ingest_service = IngestService(
            server_repo=server_repo,
            site_repo=MongoSiteRepository(mongo),
            manager_repo=manager_repo,
            classification_service=ClassificationService(
                rule_repo=rule_repo, engine=regex_engine, mongo=mongo
            ),
            health_service=HealthPolicyService(
                policy_repo=policy_repo, registry=build_default_registry(), server_repo=server_repo
            ),
            audit=AuditService(repo=MongoAuditEventRepository(mongo)),
        )
        credential_resolver = FilesystemCredentialResolver(settings.credentials_dir)

        all_managers = await manager_repo.list_all()
        managers = [m for m in all_managers if m.type == manager_type and m.enabled]
        if not managers:
            logger.warning("collector.no_managers", manager_type=manager_type.value)
            print(f"No enabled Manager documents of type {manager_type.value} found.")
            return 0

        failures = 0
        for manager in managers:
            summary = await _run_one_manager(
                manager,
                ingest_service=ingest_service,
                credential_resolver=credential_resolver,
                timeout_seconds=settings.collector_connect_timeout_seconds,
            )
            if summary is None:
                failures += 1
                print(f"manager={manager.name} FAILED (see logs)")
                continue
            print(
                f"manager={manager.name} fetched={summary.fetched} "
                f"created={summary.created} updated={summary.updated} errors={summary.errors}"
            )

        if failures:
            print(f"\n{failures}/{len(managers)} manager(s) failed.")
        return 1 if failures else 0
    finally:
        await mongo.close()


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    exit_code = asyncio.run(_run(manager_type=ManagerType(args.manager_type)))
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
