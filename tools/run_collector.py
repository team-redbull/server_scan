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
import re
from collections.abc import AsyncIterator, Awaitable, Callable

import structlog

from app.application.services.audit_service import AuditService
from app.application.services.classification_service import ClassificationService
from app.application.services.health_policy_service import HealthPolicyService
from app.application.services.ingest import IngestService, IngestSummary
from app.config import get_settings
from app.domain.enums import ManagerType
from app.domain.models.common import AuditFields
from app.domain.models.manager import Manager
from app.domain.ports.credentials import (
    CredentialResolver,
    ManagerConnection,
    ManagerNotConfiguredError,
)
from app.domain.ports.provider import ProviderServer, ServerInventoryProvider
from app.domain.services.health.metrics import build_default_registry
from app.domain.services.regex_engine import RegexModuleEngine
from app.domain.value_objects.site import parse_site_code
from app.infrastructure.credentials import EnvConnectionResolver
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
from app.infrastructure.providers.ucs_central.provider import UcsCentralProvider
from app.infrastructure.providers.ucs_manager.provider import UcsManagerProvider

logger = structlog.get_logger(__name__)

# One entry per manager type this tool actually knows how to collect
# from. A missing entry is a real gap (OpenManage, Intersight, OneView),
# not an oversight: `_build_provider` raises a clear "not implemented
# yet" for any `manager.type` without an entry here, rather than silently
# skipping that manager.
#
# UCS_MANAGER and UCS_CENTRAL are both collection sources, and which one
# to run is a deployment choice rather than a hierarchy:
#
#   UCS_MANAGER  one domain, live and authoritative. One endpoint reaches
#                exactly one domain, so a multi-domain fleet needs one
#                configured endpoint per domain — which this tool's
#                one-endpoint-per-type config cannot express.
#   UCS_CENTRAL  every registered domain in one login. The tradeoff is
#                that Central serves a *replica* of each domain's
#                inventory, and it is unconfirmed whether that replica
#                includes domain-local service profiles — the source of
#                a server's name. See `..ucs_central.provider`'s module
#                docstring; the provider logs per-domain coverage so the
#                answer is visible in the first real run.
#
# Annotated rather than inferred: with two entries mypy widens the value
# type to `object`, and the annotation is also what pins the constructor
# contract every future provider must match.
_PROVIDER_FACTORIES: dict[ManagerType, Callable[..., ServerInventoryProvider]] = {
    ManagerType.UCS_MANAGER: UcsManagerProvider,
    ManagerType.UCS_CENTRAL: UcsCentralProvider,
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


def manager_for(manager_type: ManagerType, connection: ManagerConnection) -> Manager:
    """The `Manager` document representing this deployment's single
    manager of `manager_type`.

    Derived from configuration rather than read from MongoDB: with one
    endpoint per vendor type, a stored document would be a second copy of
    what the environment already says, free to drift from it. The id is
    deterministic so re-running a collector updates the same document
    instead of accumulating one per run, and so every server it ingests
    keeps a stable `manager_id`.

    It is still written to the `managers` collection on each run — the API
    and UI resolve `Server.manager_id` through it — but as a projection of
    config, never as its source.
    """
    return Manager(
        id=f"mgr_{manager_type.value.lower()}",
        name=manager_type.value.lower().replace("_", "-"),
        type=manager_type,
        endpoint=connection.endpoint,
        enabled=True,
        audit=AuditFields.new(),
    )


class _NameFilteredProvider:
    """Drops every server whose name doesn't match `pattern` before it
    reaches the pipeline — `INVENTORY_COLLECTOR_NAME_PATTERN`.

    A wrapper here rather than a guard inside `IngestService` because
    *which servers to collect* is a collection concern: it belongs to the
    thing pointed at a vendor manager holding the whole datacenter, not
    to the pipeline shared with `tools/seed_inventory.py`, whose fake
    servers have no manager to be filtered out of. Wrapping also keeps
    `--dry-run` honest — it bypasses `IngestService` on purpose, so a
    filter living there would make a dry run print servers a real run
    would never write.

    Vendor-agnostic by construction: it wraps the `ServerInventoryProvider`
    Protocol, so OpenManage/OneView/Intersight inherit it the day they
    exist without a line of their own.
    """

    def __init__(self, inner: ServerInventoryProvider, pattern: str) -> None:
        self._inner = inner
        self._pattern = re.compile(pattern)
        self.provider_type = inner.provider_type

    async def health_check(self) -> None:
        await self._inner.health_check()

    async def list_servers(self) -> AsyncIterator[ProviderServer]:
        kept = skipped = 0
        async for provider_server in self._inner.list_servers():
            if self._pattern.search(provider_server.name):
                kept += 1
                yield provider_server
            else:
                skipped += 1
        # Logged unconditionally, including the all-zero case: "0 kept, 0
        # skipped" is the signature of a wrong endpoint, while "0 kept,
        # 900 skipped" is the signature of a wrong pattern, and an
        # otherwise-successful empty run looks identical without it.
        logger.info(
            "collector.name_filter_applied",
            pattern=self._pattern.pattern,
            kept=kept,
            skipped=skipped,
        )


def _filtered(provider: ServerInventoryProvider, pattern: str) -> ServerInventoryProvider:
    return _NameFilteredProvider(provider, pattern) if pattern else provider


async def _build_provider(
    manager: Manager, *, credential_resolver: CredentialResolver, timeout_seconds: float
) -> ServerInventoryProvider:
    factory = _PROVIDER_FACTORIES.get(manager.type)
    if factory is None:
        raise NotImplementedError(
            f"No collector implemented yet for manager type {manager.type!r}."
        )
    connection = credential_resolver.resolve(manager.type)
    return factory(manager=manager, credentials=connection, timeout_seconds=timeout_seconds)


async def _dry_run_one_manager(
    manager: Manager,
    *,
    credential_resolver: CredentialResolver,
    timeout_seconds: float,
    limit: int | None,
    name_pattern: str = "",
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
    provider = _filtered(
        await build(
            manager, credential_resolver=credential_resolver, timeout_seconds=timeout_seconds
        ),
        name_pattern,
    )
    print(f"\n=== {manager.name} ({manager.type.value} @ {manager.endpoint}) ===")
    if name_pattern:
        print(f"    (only servers whose name matches {name_pattern!r} are shown/collected)")

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
    credential_resolver: CredentialResolver,
    timeout_seconds: float,
    name_pattern: str = "",
) -> IngestSummary | None:
    try:
        provider = _filtered(
            await _build_provider(
                manager, credential_resolver=credential_resolver, timeout_seconds=timeout_seconds
            ),
            name_pattern,
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
        credential_resolver = EnvConnectionResolver(settings)

        # One manager per type, straight from configuration — nothing is
        # read from the `managers` collection to decide where to connect.
        try:
            connection = credential_resolver.resolve(manager_type)
        except ManagerNotConfiguredError as exc:
            logger.error("collector.not_configured", manager_type=manager_type.value)
            print(f"{exc}")
            return 2
        manager = manager_for(manager_type, connection)

        if dry_run:
            # No indexes, no ingest pipeline, no repositories at all.
            try:
                await _dry_run_one_manager(
                    manager,
                    credential_resolver=credential_resolver,
                    timeout_seconds=settings.collector_connect_timeout_seconds,
                    limit=limit,
                    name_pattern=settings.collector_name_pattern,
                )
            except Exception:
                logger.exception("collector.dry_run_failed", manager_id=manager.id)
                print(f"manager={manager.name} FAILED (see logs)")
                return 1
            return 0

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
        summary = await _run_one_manager(
            manager,
            ingest_service=ingest_service,
            credential_resolver=credential_resolver,
            timeout_seconds=settings.collector_connect_timeout_seconds,
            name_pattern=settings.collector_name_pattern,
        )
        if summary is None:
            print(f"manager={manager.name} FAILED (see logs)")
            return 1
        print(
            f"manager={manager.name} fetched={summary.fetched} "
            f"created={summary.created} updated={summary.updated} errors={summary.errors}"
        )
        return 0
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
