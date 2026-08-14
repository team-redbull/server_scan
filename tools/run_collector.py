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
import os
from collections.abc import Awaitable, Callable

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
from app.domain.value_objects.site import parse_site_code
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
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Print what each manager reports and exit without writing anything. "
            "Nothing is classified, health-evaluated, audited or upserted."
        ),
    )
    parser.add_argument(
        "--debug-xml",
        action="store_true",
        help=(
            "Dump every XML request and response ucsmsdk exchanges with the "
            "manager. Very verbose — pair it with --dry-run --limit."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="With --dry-run, stop after N servers per manager.",
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


async def _dry_run_one_manager(
    manager: Manager,
    *,
    credential_resolver: FilesystemCredentialResolver,
    timeout_seconds: float,
    limit: int | None,
    provider_factory: Callable[..., Awaitable[ServerInventoryProvider]] | None = None,
) -> int:
    """Print what `manager` reports, writing nothing. Returns the count.

    Deliberately bypasses `IngestService` entirely rather than passing it
    some no-op repository: the point of a dry run is to see what the
    *provider* produces, before classification, health evaluation and
    correlation have had a chance to reshape it. Anything printed here is
    the raw `ProviderServer` the collector would hand to the pipeline.
    """
    build = provider_factory or _build_provider
    provider = await build(
        manager, credential_resolver=credential_resolver, timeout_seconds=timeout_seconds
    )
    print(f"\n=== {manager.name} ({manager.type.value} @ {manager.endpoint}) ===")

    count = 0
    async for ps in provider.list_servers():
        if limit is not None and count >= limit:
            print(f"  … stopped at --limit {limit}")
            break
        count += 1
        site = parse_site_code(ps.name)
        print(
            f"\n[{count}] {ps.name}"
            f"\n     external_id : {ps.external_id}"
            f"\n     site (from name): {site.value if site else '— none in name'}"
            f"\n     vendor/model: {ps.vendor} / {ps.model}"
            f"\n     serial/uuid : {ps.serial} / {ps.system_uuid}"
            f"\n     cpu         : {ps.cpu_sockets} sockets, {ps.cpu_cores} cores,"
            f" {ps.cpu_threads} threads"
            f"\n     memory      : {ps.memory_total_bytes / 1024**3:.1f} GiB"
            f"\n     bmc         : {ps.bmc_address_raw or '—'} (mac {ps.bmc_mac or '—'})"
            f"\n     profile tmpl: {ps.profile_template_name or '—'}"
            f" [{ps.profile_template_external_id or '—'}]"
            f"\n     nic macs    : {', '.join(ps.nic_macs) if ps.nic_macs else '—'}"
            f"\n     attachments : {len(ps.attachments)}"
        )
        for a in ps.attachments:
            print(
                f"        fabric {a.fabric}  if={a.server_interface}"
                f"  admin={a.admin_state} oper={a.oper_state}"
                f"  peer={a.fabric_port or '—'}"
            )
    print(f"\n{manager.name}: {count} server(s) reported. Nothing was written.")
    return count


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


async def _run(
    *, manager_type: ManagerType, dry_run: bool = False, limit: int | None = None
) -> int:
    settings = get_settings()
    configure_logging(
        level=settings.log_level,
        service_name=settings.service_name,
        environment=settings.environment,
    )

    mongo = MongoClientHolder(settings)
    await mongo.connect()
    try:
        manager_repo = MongoManagerRepository(mongo)
        credential_resolver = FilesystemCredentialResolver(settings.credentials_dir)

        all_managers = await manager_repo.list_all()
        managers = [m for m in all_managers if m.type == manager_type and m.enabled]
        if not managers:
            logger.warning("collector.no_managers", manager_type=manager_type.value)
            print(f"No enabled Manager documents of type {manager_type.value} found.")
            return 0

        if dry_run:
            # No indexes, no ingest pipeline, no repositories beyond the
            # manager lookup this needed to find an endpoint at all.
            failures = 0
            for manager in managers:
                try:
                    await _dry_run_one_manager(
                        manager,
                        credential_resolver=credential_resolver,
                        timeout_seconds=settings.collector_connect_timeout_seconds,
                        limit=limit,
                    )
                except Exception:
                    logger.exception("collector.dry_run_failed", manager_id=manager.id)
                    print(f"manager={manager.name} FAILED (see logs)")
                    failures += 1
            return 1 if failures else 0

        await ensure_indexes(mongo.db)

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
    if args.debug_xml:
        # Read by `UcsManagerClient`; set here so it covers every provider
        # this run constructs.
        os.environ["INVENTORY_UCS_DUMP_XML"] = "1"
    exit_code = asyncio.run(
        _run(
            manager_type=ManagerType(args.manager_type),
            dry_run=args.dry_run,
            limit=args.limit,
        )
    )
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
