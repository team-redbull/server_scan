"""Ingestion pipeline: `ProviderServer` -> domain `Server`, upserted via
`ServerRepository`.

Correlation simplification (slice 1 scope, documented here since it's the
one deliberate shortcut in this module): a `ProviderServer` is matched
against an existing document only by `(vendor, serial_normalized)` when a
serial is present; with no serial, it is always treated as a new server.
The full identity-correlation ladder described in
`app.domain.models.server.Identity`'s docstring (system_uuid first, then
BMC MAC, then NIC MACs, then per-manager `external_id`, ...) is a later
slice's job — this keeps ingestion working end-to-end now without
pretending to solve a problem outside this module's scope.

Field ownership: every field this module writes directly is
ingestion-owned (identity, hardware, network, connectivity, name/model,
`source_provider`, `last_seen_at`). It never touches `tags` beyond what
the provider reports, and never touches `maintenance`/`openshift` at all
(those belong to their own future engines) — those two are always carried
forward verbatim from the existing document on update, never reset to
zero. `classification` and `health` are the one exception: when
`classification_service`/`health_service` are supplied (see
`IngestService.__init__`), this module calls them itself, right after
building the rest of the document and before computing `search_tokens`
(which reads `classification.installation_type`) — so a server is
classified and health-evaluated in the same write that ingests it, one
upsert per server rather than a second round-trip. Both services are
optional and default to `None` specifically so ingestion keeps working
before either engine is wired up (and so tests of this module in
isolation don't need to construct them) — with no service supplied, the
previous behavior applies: carry the existing classification/health
forward unchanged, or leave them at their zero value for a new server.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

import structlog
from pymongo.errors import DuplicateKeyError

from app.application.services.audit_service import SYSTEM_INGEST_ACTOR, AuditService
from app.application.services.pipeline import classification_from_result, health_from_state
from app.domain.enums import MediaType, Vendor
from app.domain.models.audit_event import EventType
from app.domain.models.connectivity import (
    Connectivity,
    ConnectivityAttachment,
    compute_connectivity_facts,
)
from app.domain.models.hardware import Cpu, Hardware, Memory, Power, Storage, StorageDrive
from app.domain.models.manager import Manager
from app.domain.models.network import BmcInfo, NetworkInfo
from app.domain.models.server import Identity, ProfileTemplate, Server
from app.domain.models.site import Site
from app.domain.ports.provider import ProviderServer, ServerInventoryProvider
from app.domain.ports.repository import ServerRepository
from app.domain.services.classification import ClassifiableServer
from app.domain.services.normalize import normalize_text
from app.domain.services.search_tokens import build_search_tokens
from app.domain.value_objects.bmc_address import parse_bmc_address
from app.domain.value_objects.mac_address import normalize_mac
from app.domain.value_objects.site import parse_site_code
from app.utils.ids import new_id
from app.utils.timeutil import utcnow

if TYPE_CHECKING:
    from app.application.services.classification_service import ClassificationService
    from app.application.services.health_policy_service import HealthPolicyService

logger = structlog.get_logger(__name__)


class SiteRepositoryPort(Protocol):
    """The one method `IngestService` needs from a site repository.
    Defined here (application layer) rather than in `app.domain.ports`
    because it's this service's own dependency, not a cross-cutting
    domain contract — `MongoSiteRepository` satisfies it structurally.
    """

    async def upsert(self, site: Site) -> Site: ...


class ManagerRepositoryPort(Protocol):
    async def upsert(self, manager: Manager) -> Manager: ...


@dataclass(slots=True)
class IngestSummary:
    fetched: int = 0
    created: int = 0
    updated: int = 0
    errors: int = 0


def _opt_str(value: object) -> str | None:
    return str(value) if value is not None else None


def _opt_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):  # bool is an int subclass; never intended here
        return int(value)
    if isinstance(value, int):
        return value
    return int(str(value))


def _carry_forward[T](reported: T | None, previous: T | None, *, default: T) -> T:
    """
    Resolve one optionally-reported field against what is already stored.

    A provider reports `None` for a field it could not read on this run
    (see `app.domain.ports.provider.ProviderServer`). Overwriting a stored
    value with the zero value in that case silently destroys good data —
    and, for storage, silently clears the seeded `storage.failed_drive`
    health policy. See docs/adr/0016-redfish-standalone-collector.md.

    Args:
        reported (T | None): What the provider reported, or `None` if it
            could not read the field.
        previous (T | None): The value on the existing document, or `None`
            for a server being created.
        default (T): The value for a new server whose provider reported
            nothing.

    Returns:
        T: The reported value when there is one, else the stored value,
            else `default`.
    """
    if reported is not None:
        return reported
    return previous if previous is not None else default


def _drive_from_dict(data: dict[str, object]) -> StorageDrive:
    media_raw = data.get("media_type")
    try:
        media_type = MediaType(str(media_raw)) if media_raw else MediaType.UNKNOWN
    except ValueError:
        media_type = MediaType.UNKNOWN
    return StorageDrive(
        id=str(data.get("id", "")),
        model=_opt_str(data.get("model")),
        serial=_opt_str(data.get("serial")),
        media_type=media_type,
        capacity_bytes=_opt_int(data.get("capacity_bytes")),
        health=_opt_str(data.get("health")),
    )


class IngestService:
    """Runs a full provider ingest: upsert referenced sites/managers, then
    normalize/correlate/upsert every `ProviderServer` the provider yields.
    """

    def __init__(
        self,
        *,
        server_repo: ServerRepository,
        site_repo: SiteRepositoryPort,
        manager_repo: ManagerRepositoryPort,
        classification_service: ClassificationService | None = None,
        health_service: HealthPolicyService | None = None,
        audit: AuditService | None = None,
    ) -> None:
        self._server_repo = server_repo
        self._site_repo = site_repo
        self._manager_repo = manager_repo
        self._classification_service = classification_service
        self._health_service = health_service
        self._audit = audit

    async def ingest(
        self,
        provider: ServerInventoryProvider,
        *,
        sites: Sequence[Site] = (),
        managers: Sequence[Manager] = (),
    ) -> IngestSummary:
        summary = IngestSummary()

        # Idempotent — safe to upsert the same fixed site/manager set on
        # every ingest run.
        for site in sites:
            await self._site_repo.upsert(site)
        for manager in managers:
            await self._manager_repo.upsert(manager)

        await provider.health_check()

        async for provider_server in provider.list_servers():
            summary.fetched += 1
            try:
                created = await self._ingest_one(
                    provider_server, provider_type=provider.provider_type
                )
            except Exception:
                logger.exception(
                    "ingest.server_failed",
                    external_id=provider_server.external_id,
                    vendor=provider_server.vendor,
                )
                summary.errors += 1
                continue

            if created:
                summary.created += 1
            else:
                summary.updated += 1

        logger.info(
            "ingest.completed",
            fetched=summary.fetched,
            created=summary.created,
            updated=summary.updated,
            errors=summary.errors,
        )
        return summary

    async def _find_by_vendor_serial(self, vendor: Vendor, serial_normalized: str) -> Server | None:
        page = await self._server_repo.list_page(
            filters={
                "identity.vendor": vendor.value,
                "identity.serial_normalized": serial_normalized,
            },
            search=None,
            sort="name",
            sort_desc=False,
            cursor=None,
            page_size=1,
            with_count=False,
        )
        return page.items[0] if page.items else None

    async def _ingest_one(self, ps: ProviderServer, *, provider_type: str) -> bool:
        """Returns True if a new server document was created, False if an
        existing one was updated.
        """
        # No fallback vendor. Every server arrives through a
        # vendor-specific collector, so an unrecognized value means that
        # provider is emitting something it shouldn't — a bug to surface,
        # not to paper over with an "unknown" that then pollutes every
        # per-vendor count in the UI. `ingest()`'s per-server handler
        # catches this, logs the offending string, counts it in
        # `IngestSummary.errors`, and moves to the next server.
        try:
            vendor = Vendor(ps.vendor)
        except ValueError as exc:
            raise ValueError(
                f"Provider reported unsupported vendor {ps.vendor!r} for "
                f"{ps.external_id!r}; expected one of {[v.value for v in Vendor]}."
            ) from exc

        serial_normalized = normalize_text(ps.serial)
        existing = (
            await self._find_by_vendor_serial(vendor, serial_normalized)
            if serial_normalized
            else None
        )

        server = await self._build_server(
            ps,
            vendor=vendor,
            serial_normalized=serial_normalized,
            existing=existing,
            provider_type=provider_type,
        )

        try:
            await self._server_repo.upsert(server)
        except DuplicateKeyError:
            # A concurrent/duplicate insert collided on a secondary unique
            # index (system_uuid or (vendor, serial_normalized)) between
            # our lookup and our upsert. Not fancy: look the real owner up
            # and update it in place instead of failing the whole run.
            refetched = (
                await self._find_by_vendor_serial(vendor, serial_normalized)
                if serial_normalized
                else None
            )
            if refetched is None:
                raise
            existing = refetched
            server = await self._build_server(
                ps,
                vendor=vendor,
                serial_normalized=serial_normalized,
                existing=refetched,
                provider_type=provider_type,
            )
            await self._server_repo.upsert(server)
            await self._emit_transition_events(existing, server)
            return False

        await self._emit_transition_events(existing, server)
        return existing is None

    async def _emit_transition_events(self, existing: Server | None, server: Server) -> None:
        """Ingestion runs continuously and touches `last_seen_at` on every
        server on every run, so a generic SERVER_UPDATED event would be
        pure noise — the only ingestion-driven transitions worth an audit
        entry are "this server is new" and "the engines' verdict about
        this server actually changed", which is exactly what
        `POST /servers/{id}/reclassify` and `.../health/recalculate`
        (`app.api.v1.servers`) already audit for an explicit, operator-
        triggered re-evaluation — this mirrors that same selectivity for
        the automatic path.
        """
        if self._audit is None:
            return

        if existing is None:
            await self._audit.record(
                EventType.SERVER_CREATED,
                actor=SYSTEM_INGEST_ACTOR,
                server_id=server.id,
                data={"vendor": server.identity.vendor.value, "name": server.name},
            )
            return

        if server.classification.installation_type != existing.classification.installation_type:
            await self._audit.record(
                EventType.CLASSIFICATION_CHANGED,
                actor=SYSTEM_INGEST_ACTOR,
                server_id=server.id,
                data={
                    "from": existing.classification.installation_type.value,
                    "to": server.classification.installation_type.value,
                    "matched_rule_id": server.classification.matched_rule_id,
                },
            )

        if server.health.overall != existing.health.overall:
            await self._audit.record(
                EventType.HEALTH_STATUS_CHANGED,
                actor=SYSTEM_INGEST_ACTOR,
                server_id=server.id,
                data={"from": existing.health.overall.value, "to": server.health.overall.value},
            )

    async def _build_server(
        self,
        ps: ProviderServer,
        *,
        vendor: Vendor,
        serial_normalized: str,
        existing: Server | None,
        provider_type: str,
    ) -> Server:
        now = utcnow()
        server_id = existing.id if existing is not None else new_id("server")
        created_at = existing.created_at if existing is not None else now
        revision = existing.revision + 1 if existing is not None else 1

        existing_hardware = existing.hardware if existing is not None else None

        bmc_parsed = parse_bmc_address(ps.bmc_address_raw)
        bmc_mac = normalize_mac(ps.bmc_mac)
        nic_macs = _carry_forward(
            [mac for mac in (normalize_mac(m) for m in ps.nic_macs) if mac is not None]
            if ps.nic_macs is not None
            else None,
            existing.identity.nic_macs if existing is not None else None,
            default=[],
        )

        identity = Identity(
            vendor=vendor,
            serial=ps.serial,
            serial_normalized=serial_normalized,
            system_uuid=ps.system_uuid,
            nic_macs=nic_macs,
            external_ids={ps.manager_id: ps.external_id} if ps.manager_id else {},
        )
        profile_template = ProfileTemplate(
            name=ps.profile_template_name, external_id=ps.profile_template_external_id
        )

        network = NetworkInfo(
            bmc=BmcInfo(
                address_raw=ps.bmc_address_raw,
                scheme=bmc_parsed.scheme if bmc_parsed else None,
                host=bmc_parsed.host if bmc_parsed else None,
                host_is_ip=bmc_parsed.host_is_ip if bmc_parsed else False,
                port=bmc_parsed.port if bmc_parsed else None,
                path=bmc_parsed.path if bmc_parsed else None,
                mac=bmc_mac,
            ),
            # ProviderServer carries a flat MAC list, not per-interface
            # name/speed/link-state — nothing to populate `interfaces`
            # from yet. A real collector's richer per-NIC data will need
            # `ProviderServer` extended before this can be filled in.
            interfaces=[],
        )

        attachments = [
            ConnectivityAttachment(
                type=a.type,
                provider=a.provider,
                fabric=a.fabric,
                fabric_name=a.fabric_name,
                fabric_id=a.fabric_id,
                fabric_model=a.fabric_model,
                fabric_serial=a.fabric_serial,
                server_interface=a.server_interface,
                server_port=a.server_port,
                fabric_port=a.fabric_port,
                admin_state=a.admin_state,
                oper_state=a.oper_state,
                speed_mbps=a.speed_mbps,
                last_seen=now,
            )
            for a in ps.attachments
        ]
        connectivity = Connectivity(
            attachments=attachments, facts=compute_connectivity_facts(attachments)
        )

        hardware = Hardware(
            cpu=Cpu(
                sockets=_carry_forward(
                    ps.cpu_sockets,
                    existing_hardware.cpu.sockets if existing_hardware else None,
                    default=0,
                ),
                cores=_carry_forward(
                    ps.cpu_cores,
                    existing_hardware.cpu.cores if existing_hardware else None,
                    default=0,
                ),
                threads=_carry_forward(
                    ps.cpu_threads,
                    existing_hardware.cpu.threads if existing_hardware else None,
                    default=0,
                ),
                model=_carry_forward(
                    ps.cpu_model,
                    existing_hardware.cpu.model if existing_hardware else None,
                    default=None,
                ),
            ),
            memory=Memory(
                total_bytes=_carry_forward(
                    ps.memory_total_bytes,
                    existing_hardware.memory.total_bytes if existing_hardware else None,
                    default=0,
                ),
                modules=[],
            ),
            storage=Storage(
                total_bytes=_carry_forward(
                    ps.storage_total_bytes,
                    existing_hardware.storage.total_bytes if existing_hardware else None,
                    default=0,
                ),
                drives=_carry_forward(
                    [_drive_from_dict(d) for d in ps.storage_drives]
                    if ps.storage_drives is not None
                    else None,
                    existing_hardware.storage.drives if existing_hardware else None,
                    default=[],
                ),
            ),
            # `ProviderServer` has no GPU field (see
            # `app.infrastructure.providers.fake.generator`'s docstring) —
            # nothing to populate this from until the port grows one.
            gpus=[],
            power=Power(psus=[]),
        )

        # `maintenance`/`openshift` are always carried forward verbatim —
        # this module never touches either. `classification`/`health`
        # default to the same carry-forward (or the zero value for a new
        # server) and are only overwritten below if the corresponding
        # engine service was supplied — see module docstring.
        carried_forward: dict[str, object] = {}
        if existing is not None:
            carried_forward = {
                "classification": existing.classification,
                "health": existing.health,
                "maintenance": existing.maintenance,
                "openshift": existing.openshift,
            }

        # The name is the authority on site, not the collector's config —
        # see `app.domain.value_objects.site`. `None` (a name with no site
        # token) is a real, surfaced state, never defaulted to a site.
        site_id = parse_site_code(ps.name)

        server = Server(
            _id=server_id,
            name=ps.name,
            name_normalized=normalize_text(ps.name),
            model=ps.model,
            model_normalized=normalize_text(ps.model),
            identity=identity,
            profile_template=profile_template,
            hardware=hardware,
            network=network,
            connectivity=connectivity,
            site_id=site_id,
            manager_id=ps.manager_id,
            tags=list(ps.tags),
            source_provider=provider_type,
            last_seen_at=now,
            revision=revision,
            created_at=created_at,
            updated_at=now,
            **carried_forward,
        )

        if self._classification_service is not None:
            classifiable = ClassifiableServer(
                name=ps.name,
                vendor=vendor,
                # No `manager_type` on `Server`/`ProviderServer` today (only
                # `manager_id`) — a manager-scoped classification rule
                # cannot currently match during ingest. Documented gap, not
                # silently wrong: the same limitation is already recorded
                # in `ClassificationService`'s and `HealthPolicyService`'s
                # own docstrings.
                manager_type=None,
                site_id=site_id,
                serial=ps.serial,
                model=ps.model,
            )
            result = await self._classification_service.classify_server(classifiable)
            previous_version = existing.classification.classification_version if existing else 0
            server.classification = classification_from_result(
                result, previous_version=previous_version
            )

        if self._health_service is not None:
            state = await self._health_service.evaluate_server(server)
            server.health = health_from_state(state)

        server.search_tokens = build_search_tokens(server)
        return server
