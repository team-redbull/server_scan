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
from dataclasses import dataclass

import structlog

from app.application.services.audit_service import AuditService
from app.application.services.classification_service import ClassificationService
from app.application.services.health_policy_service import HealthPolicyService
from app.application.services.ingest import IngestService, IngestSummary
from app.config import get_settings
from app.config.settings import Settings
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
from app.domain.value_objects.bmc_address import parse_bmc_address
from app.domain.value_objects.site import parse_site_code
from app.infrastructure.credentials import EnvConnectionResolver
from app.infrastructure.credentials.env import resolve_login
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
from app.infrastructure.providers.intersight.provider import IntersightProvider
from app.infrastructure.providers.redfish.provider import RedfishStandaloneProvider
from app.infrastructure.providers.redfish.targets import load_targets
from app.infrastructure.providers.ucs_central.provider import UcsCentralProvider

logger = structlog.get_logger(__name__)

_DEBUG_HTTP_VAR = "INVENTORY_REDFISH_DEBUG_HTTP"


def _debug_http_enabled() -> bool:
    """
    Report whether `--debug-http` was passed.

    Threaded through the environment rather than the factory signature so
    it reaches every client the run constructs, matching how `--debug-xml`
    already reaches `UcsManagerClient`.

    Returns:
        bool: True when HTTP tracing is on.
    """
    return os.environ.get(_DEBUG_HTTP_VAR) == "1"


def _ucs_central_provider(
    *,
    manager: Manager,
    credentials: ManagerConnection,
    timeout_seconds: float,
    settings: Settings,
) -> ServerInventoryProvider:
    """Central for the domain list, each domain's own UCS Manager for the
    servers — see `_PROVIDER_FACTORIES`' comment below.
    """
    # Raised here, before any connection is attempted, so a half-configured
    # deployment gets the variable names to set rather than a per-domain
    # login failure that reads like bad credentials. `_run` turns
    # `ManagerNotConfiguredError` into exit code 2 with this message.
    domain_login = resolve_login(settings, ManagerType.UCS_MANAGER)
    return UcsCentralProvider(
        manager=manager,
        credentials=credentials,
        timeout_seconds=timeout_seconds,
        domain_login=domain_login,
        # The same pattern `_NameFilteredProvider` applies, reused only to
        # skip domains that certainly hold nothing of ours. It never
        # decides which *servers* are ingested — see `domains_to_collect`.
        name_pattern=settings.collector_name_pattern,
        concurrency=settings.ucs_central_domain_concurrency,
    )


# One entry per manager type this tool can be pointed at. A missing entry
# is a real gap (OpenManage, Intersight, OneView), not an oversight:
# `_build_provider` raises a clear "not implemented yet" for any
# `manager.type` without one, rather than silently skipping that manager.
#
# Cisco has exactly one entry point, and it is UCS_CENTRAL:
#
#   UCS_CENTRAL  Central enumerates the registered domains and their
#                service-profile names; each domain's real inventory is
#                then read live from that domain's own UCS Manager through
#                the emulator-validated `..ucs_manager` path. One Central
#                login (INVENTORY_UCS_CENTRAL_*) plus one UCS Manager login
#                valid fleet-wide (INVENTORY_UCS_MANAGER_USERNAME/
#                _PASSWORD). There is no INVENTORY_UCS_MANAGER_IP: Central
#                supplies each domain's address.
#
# UCS_MANAGER is deliberately absent. `UcsManagerProvider` is not gone —
# it is the engine this collector drives once per domain — but it has no
# endpoint of its own to be configured with any more, so pointing this
# tool at UCS_MANAGER is a usage error rather than a missing feature, and
# `_build_provider` says so in its own words instead of claiming no
# collector exists.
#
# REDFISH_STANDALONE is the one entry that names no manager at all: it
# reaches machines no aggregator owns, one BMC at a time, from an
# inventory file. See docs/adr/0016-redfish-standalone-collector.md.
#
# Annotated rather than inferred: mypy would otherwise widen the value
# type, and the annotation is also what pins the factory contract every
# future provider must match.
def _redfish_provider(
    *,
    manager: Manager,
    credentials: ManagerConnection,
    timeout_seconds: float,
    settings: Settings,
) -> ServerInventoryProvider:
    """The standalone Redfish collector — one BMC at a time, from a file.

    `timeout_seconds` is deliberately unused. This collector splits
    connect from read (`INVENTORY_REDFISH_CONNECT_TIMEOUT_SECONDS` /
    `_READ_TIMEOUT_SECONDS`), because one value cannot serve both a fleet
    with dead hosts in it and a BMC that answers slowly. Named here so a
    parameter this factory ignores is documented rather than surprising.
    """
    return RedfishStandaloneProvider(
        manager=manager,
        # Parsed and fully validated before any connection is opened: a
        # fan-out collector must not find a typo on host 380 of 400, and a
        # credential must never reach a host a mistake put in front of.
        targets=load_targets(
            inventory_path=settings.redfish_inventory_file,
            credentials_path=settings.redfish_credentials_file,
            fallback_login=_optional_login(settings, ManagerType.REDFISH_STANDALONE),
            ca_bundle=settings.redfish_ca_bundle or None,
        ),
        connect_timeout=settings.redfish_connect_timeout_seconds,
        read_timeout=settings.redfish_read_timeout_seconds,
        host_budget_seconds=settings.redfish_host_budget_seconds,
        run_budget_seconds=settings.redfish_run_budget_seconds,
        fleet_concurrency=settings.redfish_fleet_concurrency,
        auth_failure_threshold=settings.redfish_auth_failure_threshold,
        auth_failure_budget=settings.redfish_auth_failure_budget,
        tls_min_version=settings.redfish_tls_min_version,
        debug_http=_debug_http_enabled(),
    )


def _intersight_provider(
    *,
    manager: Manager,
    credentials: ManagerConnection,
    timeout_seconds: float,
    settings: Settings,
) -> ServerInventoryProvider:
    """The Intersight collector — one endpoint, fleet-wide list queries.

    `timeout_seconds` is the connect timeout only. Reading one page of a
    fleet-wide query is a different question from reaching the endpoint
    at all, so it has its own setting, the same split the Redfish
    collector makes.
    """
    modes = tuple(
        mode.strip() for mode in settings.intersight_management_modes.split(",") if mode.strip()
    )
    # `ManagerConnection` carries a username/password pair because most
    # vendors have one. Intersight does not: the resolver filled those two
    # slots from INVENTORY_INTERSIGHT_API_KEY_ID/_PEM, and they are named
    # for what they are from here on.
    return IntersightProvider(
        manager=manager,
        endpoint=credentials.endpoint,
        api_key_id=credentials.username,
        api_key_pem=credentials.password,
        connect_timeout=timeout_seconds,
        read_timeout=settings.intersight_read_timeout_seconds,
        ca_bundle=settings.intersight_ca_bundle or None,
        page_size=settings.intersight_page_size,
        management_modes=modes,
        run_budget_seconds=settings.intersight_run_budget_seconds,
        debug_http=_debug_http_enabled(),
    )


def _optional_login(settings: Settings, manager_type: ManagerType) -> tuple[str, str] | None:
    """A type's fleet-wide login, or None when it has none configured.

    Unlike `resolve_login`, a missing value is not an error here: an
    estate where every BMC has its own account configures no fleet-wide
    fallback at all, and `load_targets` then produces a far better message
    naming the specific host whose credential could not be resolved.
    """
    try:
        return resolve_login(settings, manager_type)
    except ManagerNotConfiguredError:
        return None


# Types reached without a single configured endpoint. Both resolve a
# login only; their addresses come from elsewhere at runtime — UCS Central
# reports its domains, and the Redfish collector reads an inventory file.
# `EnvConnectionResolver.resolve` would otherwise demand an `_IP` variable
# that deliberately does not exist.
_ENDPOINTLESS_TYPES = frozenset({ManagerType.REDFISH_STANDALONE})

# `INVENTORY_COLLECTOR_NAME_PATTERN` is not applied to these. The pattern
# exists because a vendor manager holds the whole datacenter and the name
# is the only discriminator; a standalone collector's inventory file is
# already that filter, and a far more precise one. Applying `^ocp` over a
# name a BMC does not know would discard every host the operator listed.
_UNFILTERED_TYPES = frozenset({ManagerType.REDFISH_STANDALONE})


_PROVIDER_FACTORIES: dict[ManagerType, Callable[..., ServerInventoryProvider]] = {
    ManagerType.UCS_CENTRAL: _ucs_central_provider,
    ManagerType.REDFISH_STANDALONE: _redfish_provider,
    # INTERSIGHT is in neither `_ENDPOINTLESS_TYPES` nor
    # `_UNFILTERED_TYPES`, deliberately: it has a real configured
    # endpoint, and the servers it reports carry the `ocp4-...` names
    # `INVENTORY_COLLECTOR_NAME_PATTERN` exists to filter. See
    # docs/adr/0017-intersight-collector.md.
    ManagerType.INTERSIGHT: _intersight_provider,
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
        "--debug-http",
        action="store_true",
        help=(
            "Log one line per Redfish request: method, path and status only. "
            "Headers and bodies are never logged, and the session exchange is "
            "skipped entirely rather than redacted."
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

    @property
    def collection_errors(self) -> tuple[str, ...]:
        """Pass the wrapped collector's partial failures straight through.

        Without this the wrapper would swallow them and every filtered run
        would look complete. Providers that report none are treated as
        having none.
        """
        return collection_errors_of(self._inner)

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


def collection_errors_of(provider: object) -> tuple[str, ...]:
    """Partial failures a collector recorded, or none if it reports any.

    Read reflectively rather than added to the `ServerInventoryProvider`
    Protocol: only a collector that fans out over several endpoints can
    partially fail, so requiring the attribute of every provider — the
    fake seeder included — would be ceremony for a single vendor's shape.

    Args:
        provider (object): Any collector, wrapped or not.

    Returns:
        tuple[str, ...]: One message per failure, empty if the run was
            complete or the provider does not track this.
    """
    return tuple(getattr(provider, "collection_errors", ()) or ())


async def _build_provider(
    manager: Manager,
    *,
    credential_resolver: CredentialResolver,
    timeout_seconds: float,
    settings: Settings | None = None,
) -> ServerInventoryProvider:
    """`settings` is threaded in rather than read here so a caller (and a
    test) can decide which collector variant it is exercising. It defaults
    to the process-wide settings for the callers that have no opinion.
    """
    factory = _PROVIDER_FACTORIES.get(manager.type)
    if factory is None:
        # UCS Manager gets its own message: the collector for it very much
        # exists and runs on every Central run, it just has no endpoint of
        # its own to be pointed at. "Not implemented yet" would send an
        # operator looking for code that is already there.
        if manager.type is ManagerType.UCS_MANAGER:
            raise NotImplementedError(
                "UCS Manager has no collector entry point of its own — it is collected "
                "through `--manager-type UCS_CENTRAL`, which discovers every registered "
                "domain's address from UCS Central and logs into each one with "
                "INVENTORY_UCS_MANAGER_USERNAME/_PASSWORD."
            )
        raise NotImplementedError(
            f"No collector implemented yet for manager type {manager.type!r}."
        )
    connection = (
        ManagerConnection(endpoint="", username="", password="")
        if manager.type in _ENDPOINTLESS_TYPES
        else credential_resolver.resolve(manager.type)
    )
    return factory(
        manager=manager,
        credentials=connection,
        timeout_seconds=timeout_seconds,
        settings=settings if settings is not None else get_settings(),
    )


# What a dry run prints for a field the provider could not read, as
# distinct from `—` for a field it read and found empty. See
# `app.domain.ports.provider.ProviderServer`.
_UNREAD = "not read"


def _bmc_host(address_raw: str | None) -> str:
    """
    The BMC's address as an operator reads it: host only.

    The scheme, port and Redfish path a collector reports are protocol
    detail nobody checks by eye, and they push the one thing that matters
    — the address you would ping or open — off to the middle of a line.
    The full URI is still what gets stored, for the Metal3 `BareMetalHost`
    round-trip.

    Args:
        address_raw (str | None): The collector's raw BMC address.

    Returns:
        str: The host, the raw string when it cannot be parsed, or an em
            dash when there is none.
    """
    parsed = parse_bmc_address(address_raw)
    if parsed is None:
        return "—"
    return parsed.host or parsed.raw


def _or_unread(value: object) -> str:
    """
    Render an optionally-reported value for the dry-run print.

    Args:
        value (object): The reported value, or `None` if the provider
            could not read it.

    Returns:
        str: The value as text, or `"not read"` for `None`.
    """
    return _UNREAD if value is None else str(value)


def _format_capacity(capacity_bytes: int) -> str:
    """
    Render a byte count as GiB, or TiB once it is large enough that GiB
    stops being readable at a glance.

    Args:
        capacity_bytes (int): The capacity to render, in bytes.

    Returns:
        str: e.g. `"512.0 GiB"` below 1024 GiB, `"9.6 TiB"` at or above it.
    """
    gib = capacity_bytes / 1024**3
    if gib >= 1024:
        return f"{gib / 1024:.1f} TiB"
    return f"{gib:.1f} GiB"


async def _dry_run_one_manager(
    manager: Manager,
    *,
    credential_resolver: CredentialResolver,
    timeout_seconds: float,
    limit: int | None,
    name_pattern: str = "",
    settings: Settings | None = None,
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
            manager,
            credential_resolver=credential_resolver,
            timeout_seconds=timeout_seconds,
            settings=settings,
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
        site = parse_site_code(ps.name) or parse_site_code(ps.profile_dn)
        memory = (
            f"{ps.memory_total_bytes / 1024**3:.1f} GiB"
            if ps.memory_total_bytes is not None
            else _UNREAD
        )
        storage = (
            _format_capacity(ps.storage_total_bytes)
            if ps.storage_total_bytes is not None
            else _UNREAD
        )
        drive_count = _or_unread(None if ps.storage_drives is None else len(ps.storage_drives))
        macs = _UNREAD if ps.nic_macs is None else (", ".join(ps.nic_macs) or "—")
        print(
            f"\n[{count}] {ps.name}"
            f"\n     external_id : {ps.external_id}"
            f"\n     site (from name): {site.value if site else '— none in name'}"
            f"\n     vendor/model: {ps.vendor} / {ps.model}"
            f"\n     serial/uuid : {ps.serial} / {ps.system_uuid}"
            f"\n     cpu         : {_or_unread(ps.cpu_sockets)} sockets,"
            f" {_or_unread(ps.cpu_cores)} cores,"
            f" {_or_unread(ps.cpu_threads)} threads ({ps.cpu_model or 'model unknown'})"
            f"\n     memory      : {memory}"
            f"\n     storage     : {storage} total across {drive_count} drive(s)"
            f"\n     bmc         : {_bmc_host(ps.bmc_address_raw)} (mac {ps.bmc_mac or '—'})"
            f"\n     profile     : {ps.profile_dn or '—'}"
            f"\n     profile tmpl: {ps.profile_template_name or '—'}"
            f" [{ps.profile_template_external_id or '—'}]"
            f"\n     nic macs    : {macs}"
            f"\n     attachments : {len(ps.attachments)}"
            f"\n     gpus        : {_or_unread(None if ps.gpus is None else len(ps.gpus))}"
        )
        for a in ps.attachments:
            print(
                f"        [{a.interface_kind:8}] fabric {a.fabric}  if={a.server_interface}"
                f"  admin={a.admin_state} oper={a.oper_state}"
                f"  peer={a.fabric_port or '—'}"
                f"  FI model/serial={a.fabric_model or '—'}/{a.fabric_serial or '—'}"
            )
        for drive in ps.storage_drives or ():
            capacity_bytes = drive.get("capacity_bytes")
            size = (
                _format_capacity(capacity_bytes)
                if isinstance(capacity_bytes, int)
                else "size unknown"
            )
            print(
                f"        disk {drive.get('id')}  {drive.get('model') or '—'}"
                f"  serial={drive.get('serial') or '—'}"
                f"  {drive.get('media_type')}  {size}  health={drive.get('health')}"
            )
        for gpu in ps.gpus or ():
            gpu_memory = gpu.get("memory_bytes")
            gpu_size = (
                _format_capacity(gpu_memory) if isinstance(gpu_memory, int) else "VRAM unknown"
            )
            temp = gpu.get("temperature_celsius")
            power = gpu.get("power_watts")
            print(
                f"        gpu {gpu.get('model') or '—'}  vendor={gpu.get('vendor') or '—'}"
                f"  serial={gpu.get('serial') or '—'}"
                f"  {gpu_size} ({gpu.get('memory_type') or 'memory type unknown'})"
                f"  ecc={gpu.get('ecc_mode_enabled')}"
                f"  errors={gpu.get('correctable_error_count')}c/"
                f"{gpu.get('uncorrectable_error_count')}u"
                f"  temp={f'{temp:.0f}°C' if isinstance(temp, (int, float)) else '—'}"
                f"  power={f'{power:.0f}W' if isinstance(power, (int, float)) else '—'}"
                f"  health={gpu.get('health')}"
            )
    print(f"\n{manager.name}: {count} server(s) reported. Nothing was written.")
    return count


@dataclass(frozen=True, slots=True)
class _RunOutcome:
    """What one manager's run produced, and what it could not reach.

    `summary` alone cannot express a partial run: a domain that failed
    contributes no servers and no ingest errors, so its absence is
    invisible in the counts.

    Attributes:
        summary (IngestSummary): Fetched/created/updated/error counts.
        collection_errors (tuple[str, ...]): One message per endpoint the
            collector could not reach, empty for a complete run.
    """

    summary: IngestSummary
    collection_errors: tuple[str, ...]


async def _run_one_manager(
    manager: Manager,
    *,
    ingest_service: IngestService,
    credential_resolver: CredentialResolver,
    timeout_seconds: float,
    name_pattern: str = "",
    settings: Settings | None = None,
) -> _RunOutcome | None:
    try:
        provider = _filtered(
            await _build_provider(
                manager,
                credential_resolver=credential_resolver,
                timeout_seconds=timeout_seconds,
                settings=settings,
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
        # `managers=[manager]` is what actually writes the `Manager`
        # projection `manager_for()` builds. Without it every collected
        # server carried a `manager_id` pointing at a document that was
        # never created — see docs/adr/0016.
        summary = await ingest_service.ingest(provider, managers=[manager])
        return _RunOutcome(summary=summary, collection_errors=collection_errors_of(provider))
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
            if manager_type in _ENDPOINTLESS_TYPES:
                # No `_IP` variable exists for these, so `resolve()` would
                # exit 2 naming a variable that cannot be set. The
                # `Manager` projection carries the inventory path instead,
                # which is the most informative answer to "where did these
                # servers come from".
                connection = ManagerConnection(
                    endpoint=settings.redfish_inventory_file,
                    username="",
                    password="",
                )
            else:
                connection = credential_resolver.resolve(manager_type)
            # Pre-flight for the one collector that needs a *second* set of
            # credentials: UCS Central also logs into each domain's UCS
            # Manager. Checked here, beside the endpoint resolution, so a
            # half-configured deployment exits 2 naming the variables to set
            # — the provider factory raises the same error later, but by
            # then the dry-run/ingest paths have turned it into a generic
            # "FAILED (see logs)" exit 1.
            if manager_type is ManagerType.UCS_CENTRAL:
                resolve_login(settings, ManagerType.UCS_MANAGER)
        except ManagerNotConfiguredError as exc:
            logger.error("collector.not_configured", manager_type=manager_type.value)
            print(f"{exc}")
            return 2
        manager = manager_for(manager_type, connection)
        # Computed once, so the reason lives in one place rather than
        # being duplicated at both call sites below.
        name_pattern = "" if manager_type in _UNFILTERED_TYPES else settings.collector_name_pattern

        if dry_run:
            # No indexes, no ingest pipeline, no repositories at all.
            try:
                await _dry_run_one_manager(
                    manager,
                    credential_resolver=credential_resolver,
                    timeout_seconds=settings.collector_connect_timeout_seconds,
                    limit=limit,
                    name_pattern=name_pattern,
                    settings=settings,
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
        outcome = await _run_one_manager(
            manager,
            ingest_service=ingest_service,
            credential_resolver=credential_resolver,
            timeout_seconds=settings.collector_connect_timeout_seconds,
            name_pattern=name_pattern,
            settings=settings,
        )
        if outcome is None:
            print(f"manager={manager.name} FAILED (see logs)")
            return 1

        summary = outcome.summary
        print(
            f"manager={manager.name} fetched={summary.fetched} "
            f"created={summary.created} updated={summary.updated} errors={summary.errors}"
        )
        if outcome.collection_errors or summary.errors:
            # Exit 3, not 0: some servers were written, but this run did not
            # see the whole fleet. Reported as success it is indistinguishable
            # from a healthy run against a smaller estate, which is how a bad
            # credential on one domain stays invisible for weeks.
            logger.error(
                "collector.partial_run",
                manager_id=manager.id,
                unreachable=len(outcome.collection_errors),
                ingest_errors=summary.errors,
            )
            print(f"manager={manager.name} PARTIAL — this run did not see the whole fleet:")
            for message in outcome.collection_errors:
                print(f"  - {message}")
            if summary.errors:
                print(f"  - {summary.errors} server(s) failed to ingest (see logs)")
            return 3
        return 0
    finally:
        await mongo.close()


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    if args.debug_xml:
        # Read by `UcsManagerClient`; set here so it covers every provider
        # this run constructs.
        os.environ["INVENTORY_UCS_DUMP_XML"] = "1"
    if args.debug_http:
        os.environ[_DEBUG_HTTP_VAR] = "1"
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
