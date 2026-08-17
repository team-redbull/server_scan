"""`ServerInventoryProvider` for Cisco UCS Central — every registered UCS
Manager domain in one collector run.

This is the **only** Cisco entry point. A UCS Manager connection reaches
exactly one domain, and one endpoint per `ManagerType` is all the
configuration model expresses, so a fleet spread over several domains was
unreachable past the first — which is why `UCS_MANAGER` has no configured
endpoint of its own any more (`app.infrastructure.credentials.env`) and no
entry in `tools.run_collector._PROVIDER_FACTORIES`. `..ucs_manager` is not
gone: it is the engine this collector drives once per domain, kept because
its data path is the one validated against real hardware (ADR-0009), not
because it is still separately runnable.

## Central is a directory here, not an inventory source

Central is asked exactly two questions:

  1. `computeSystem` — which domains are registered, at what address, and
     what Central believes each one holds.
  2. `lsServer` — which service-profile names live in which domain, used
     *only* to skip domains that certainly hold nothing of ours.

Everything that ends up in a `ProviderServer` then comes from that
domain's own UCS Manager, through `..ucs_manager.provider.
UcsManagerProvider` unchanged.

That split is deliberate. Central serves a *replica* of each domain's
inventory, and reading servers out of the replica was the obvious design —
nine domain-wide queries, one login, cheapest possible run. What it cannot
offer is confidence: ADR-0014 could establish only that `ucscsdk` *models*
`processorUnit`, `storageLocalDisk`, `adaptorHostEthIf` and the rest, not
that a given Central deployment populates them, and its central open
question — whether Central replicates domain-*local* service profiles, the
source of a UCS server's name — was never settled by a live run. Driving
each domain's own UCS Manager instead means every field arrives over the
one Cisco data path validated end to end against a live UCS Platform
Emulator (ADR-0009), where five real defects surfaced that no amount of
schema reading had exposed. Live, not replicated, so per-domain
replication lag stops mattering to the data as well.

## Why one login reaches every domain

Central hands out each registered domain's address (`ComputeSystem.
address`; Cisco's own `ucscsdk/utils/ucscdomain.py` filters on exactly
that property in `get_domain()`), and a single UCS Manager service account
is valid across the domains of one fleet — which is why there is no
`INVENTORY_UCS_MANAGER_IP` at all and
`app.infrastructure.credentials.env.resolve_login` exists to ask for a
login without an endpoint. The Central -> `lsServer`/`ComputeSystem` ->
per-domain UCS Manager hop is the same shape `app.domain.models.manager`
already documents as the one real hierarchy Cisco UCS has.

## Cost

Two Central queries, then per collected domain one login plus the ~9
domain-wide queries `UcsManagerProvider` issues. That per-domain cost is
flat in server count — a domain holding 500 servers costs the same as one
holding 10 — so the only levers are *how many domains* get contacted,
which is what the `lsServer` pruning below is for, and how many at once,
which is what `concurrency` is for.

Notably this is *not* the shape a naive port of the same idea takes: the
obvious implementation resolves each matching profile's `pn_dn` with a
`query_dn` and then walks `query_children` for board, CPUs, controllers
and disks — five-plus round trips *per server*, on top of the per-domain
login. The domain-wide-query-plus-client-side-join that
`UcsManagerProvider` already implements collapses that into a constant
number of requests per domain, and `..ucs_common.group_by_owning_server_dn`
is what makes the join exact.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass, replace
from typing import Any

import structlog

from app.domain.enums import ManagerType
from app.domain.models.manager import Manager
from app.domain.ports.credentials import ManagerConnection
from app.domain.ports.provider import ProviderServer
from app.infrastructure.providers.ucs_central.client import UcsCentralClient
from app.infrastructure.providers.ucs_common import TEMPLATE_TYPES
from app.infrastructure.providers.ucs_manager.provider import UcsManagerProvider

logger = structlog.get_logger(__name__)

_PROVIDER_TYPE = ManagerType.UCS_CENTRAL.value

# UCS Manager roots its whole managed-object tree at `sys`; UCS Central
# roots each registered domain's copy at `compute/sys-<domainId>`. See
# `ucscsdk`'s own `docs/ucscsdk_ug.rst`, which spells out
# `compute/sys-1009/chassis-1`.
_UCSM_ROOT = "sys/"
_CENTRAL_ROOT = "compute/sys-{domain_id}/"


def domain_id_from_dn(dn: str) -> str | None:
    """The registered domain a `compute/sys-<domainId>/...` DN belongs to.

    Returns `None` for anything not under a `computeSystem`, which is how
    global objects (an org's service profiles, for one) are told apart
    from per-domain inventory.

    Used by `tools.verify_ucs_central`, which queries Central's replica
    directly to answer whether it holds domain-local service profiles —
    the question this collector routes around rather than settles.
    """
    parts = str(dn).split("/")
    if len(parts) < 2 or not parts[1].startswith("sys-"):
        return None
    return parts[1][len("sys-") :] or None


@dataclass(frozen=True, slots=True)
class DomainTarget:
    """One registered UCS Manager domain, as Central describes it."""

    domain_id: str
    name: str
    endpoint: str


def central_external_id(external_id: str, *, domain_id: str) -> str:
    """Re-root a UCS Manager DN into the UCS Central DN for the same object.

    **UCS Manager DNs are domain-local and therefore collide.** Every
    domain in the fleet has a `sys/chassis-1/blade-1`. Servers collected
    here all carry `manager_id = mgr_ucs_central` (one `Manager` document
    per type — `tools.run_collector.manager_for`), so their external ids
    land in one `Server.external_ids[mgr_ucs_central]` namespace where an
    un-rooted DN identifies several machines at once. Identity resolution
    happens on vendor+serial (`app.application.services.ingest`), so this
    would not merge two servers — it would just make the recorded external
    id useless for saying which one, and `domain_id_from_dn` above could
    no longer recover the owning domain.

    Anything not under `sys/` is returned unchanged: org-rooted DNs
    (`org-root/ls-...`, which is what `profile_template_external_id`
    carries) are global in Central and already correct.
    """
    if not external_id.startswith(_UCSM_ROOT):
        return external_id
    return _CENTRAL_ROOT.format(domain_id=domain_id) + external_id[len(_UCSM_ROOT) :]


def _profiles_by_key(ls_servers: Iterable[Any]) -> dict[str, list[str]]:
    """Service-profile names grouped by the domain Central says they are in.

    `LsServer.domain` is the link: the installed `ucscsdk` carries
    `domain`, `domain_dn`, `domain_group` and `domain_group_dn` on
    `LsServer`, and `domain` is the one that names the UCS Manager a
    profile lives on — which is also the value the user's existing UCS
    operator opens its second session against, as
    `app.domain.models.manager`'s docstring records.

    Templates are dropped: `lsServer` carries both real profiles and the
    templates they derive from, told apart only by `type` (there is no
    separate template class in either SDK — see `..ucs_common`), and a
    template's name is not a server's name.
    """
    grouped: dict[str, list[str]] = {}
    for mo in ls_servers:
        if str(getattr(mo, "type", "") or "") in TEMPLATE_TYPES:
            continue
        key = str(getattr(mo, "domain", "") or "").strip()
        if not key:
            # An unassociated or domain-less profile says nothing about
            # which domains are worth contacting.
            continue
        name = str(getattr(mo, "name", "") or "")
        if name:
            grouped.setdefault(key, []).append(name)
    return grouped


def domains_to_collect(
    domains: Iterable[Any],
    ls_servers: Iterable[Any],
    *,
    name_pattern: str,
) -> tuple[list[DomainTarget], list[DomainTarget]]:
    """Split Central's registered domains into `(to_collect, skipped)`.

    Skipping is a **pure optimisation and nothing else**. Which servers
    actually get ingested is decided in exactly one place —
    `tools.run_collector._NameFilteredProvider`, which every vendor's
    collector goes through — and this function must never be stricter than
    it is, which is why both use `re.search` (so `^ocp` means "starts
    with" because the operator wrote the anchor, not because the code
    added one).

    The rule that matters: **a domain whose profiles Central does not
    report is collected, never skipped.** ADR-0014's open question is
    precisely whether Central replicates domain-local service profiles;
    pruning on missing evidence would silently drop exactly the domains
    that question is about, and the symptom would be a mysteriously small
    inventory rather than an error. Absence of evidence gets a round trip,
    not a guess.
    """
    profiles = _profiles_by_key(ls_servers)
    pattern = re.compile(name_pattern) if name_pattern else None

    to_collect: list[DomainTarget] = []
    skipped: list[DomainTarget] = []
    for mo in domains:
        name = str(getattr(mo, "name", "") or "").strip()
        endpoint = str(getattr(mo, "address", "") or "").strip() or name
        target = DomainTarget(
            domain_id=str(getattr(mo, "id", "") or ""),
            name=name,
            endpoint=endpoint,
        )
        if not endpoint:
            # Nothing to connect to. Skipped rather than attempted, so the
            # log says "no address" instead of an opaque DNS failure.
            logger.warning(
                "ucs_central.domain_without_address",
                domain_id=target.domain_id,
                domain_name=target.name,
            )
            skipped.append(target)
            continue

        # Central may key a profile's `domain` by the domain's name or its
        # address depending on how it was registered, and `id` is the last
        # resort; try all three rather than assume one.
        known = profiles.get(name) or profiles.get(endpoint) or profiles.get(target.domain_id) or []
        if pattern is not None and known and not any(pattern.search(n) for n in known):
            skipped.append(target)
            continue
        to_collect.append(target)

    return to_collect, skipped


class UcsCentralProvider:
    provider_type = _PROVIDER_TYPE

    def __init__(
        self,
        *,
        manager: Manager,
        credentials: ManagerConnection,
        timeout_seconds: float,
        domain_login: tuple[str, str],
        name_pattern: str = "",
        concurrency: int = 4,
        client_factory: Callable[[], Any] | None = None,
        domain_provider_factory: Callable[[DomainTarget], Any] | None = None,
    ) -> None:
        if not manager.endpoint:
            raise ValueError(f"Manager {manager.id!r} has no endpoint configured.")
        self._endpoint: str = manager.endpoint
        self._manager = manager
        self._credentials = credentials
        self._timeout_seconds = timeout_seconds
        self._domain_login = domain_login
        self._name_pattern = name_pattern
        # A domain is one blocking SDK call parked in a worker thread, so
        # this bounds threads as much as it bounds sockets.
        self._concurrency = max(1, concurrency)
        # Both widened to the injection point's own type: the defaults are
        # concrete, but a test substitutes a stand-in and the attribute has
        # to accept either.
        self._client_factory: Callable[[], Any] = client_factory or self._new_client
        self._domain_provider_factory: Callable[[DomainTarget], Any] = (
            domain_provider_factory or self._new_domain_provider
        )

    def _new_client(self) -> UcsCentralClient:
        return UcsCentralClient(
            endpoint=self._endpoint,
            username=self._credentials.username,
            password=self._credentials.password,
            timeout_seconds=self._timeout_seconds,
        )

    def _new_domain_provider(self, target: DomainTarget) -> UcsManagerProvider:
        username, password = self._domain_login
        return UcsManagerProvider(
            # The *same* manager id, with only the endpoint swapped: every
            # server collected this way belongs to the one `mgr_ucs_central`
            # document `tools.run_collector.manager_for` writes, and the
            # owning domain stays recoverable from `external_id`. Building a
            # `Manager` per domain instead would break that tool's
            # one-document-per-type contract for no gain.
            manager=self._manager.model_copy(update={"endpoint": target.endpoint}),
            credentials=ManagerConnection(
                endpoint=target.endpoint, username=username, password=password
            ),
            timeout_seconds=self._timeout_seconds,
        )

    async def health_check(self) -> None:
        # Central only. A per-domain login here would multiply the fleet's
        # session count for information the run itself is about to get.
        client = self._client_factory()
        try:
            await client.login()
        finally:
            await client.logout()

    async def _plan(self) -> tuple[list[DomainTarget], dict[str, Any]]:
        """The two Central queries, the domain list they produce, and the
        `computeSystem` MOs themselves — `_log_domains` reports what
        Central *believes* each domain holds against what that domain's
        own UCS Manager actually returned.
        """
        client = self._client_factory()
        try:
            await client.login()
            domains = await client.query_classid("computeSystem")
            ls_servers = await client.query_classid("lsServer")
        finally:
            await client.logout()

        if not domains:
            logger.warning(
                "ucs_central.no_domains",
                endpoint=self._endpoint,
                hint=(
                    "UCS Central reported no registered UCS Manager domains, so there is "
                    "nothing to collect from. Check that domains are registered and that "
                    "this account can read them."
                ),
            )

        to_collect, skipped = domains_to_collect(
            domains, ls_servers, name_pattern=self._name_pattern
        )
        logger.info(
            "ucs_central.domain_plan",
            registered=len(domains),
            collecting=len(to_collect),
            skipping=len(skipped),
            name_pattern=self._name_pattern or None,
            concurrency=self._concurrency,
        )
        for target in skipped:
            logger.info(
                "ucs_central.domain_skipped",
                domain_id=target.domain_id,
                domain_name=target.name,
                endpoint=target.endpoint or None,
                reason=(
                    "no address"
                    if not target.endpoint
                    else f"no service profile matches {self._name_pattern!r}"
                ),
            )

        # A `domain` key on some profile that names no registered domain is
        # inventory we were told about but cannot reach — the one case the
        # loop above cannot surface, since it iterates registered domains.
        known_keys = {t.name for t in (*to_collect, *skipped)}
        known_keys |= {t.endpoint for t in (*to_collect, *skipped)}
        known_keys |= {t.domain_id for t in (*to_collect, *skipped)}
        unmatched = sorted(set(_profiles_by_key(ls_servers)) - known_keys)
        if unmatched:
            logger.warning(
                "ucs_central.profiles_in_unregistered_domain",
                domains=unmatched,
                hint=(
                    "UCS Central reports service profiles whose domain matches no "
                    "registered computeSystem, so those servers cannot be collected."
                ),
            )

        domain_mo_by_id = {str(getattr(mo, "id", "") or ""): mo for mo in domains}
        return to_collect, domain_mo_by_id

    async def _collect_domain(
        self, target: DomainTarget, sem: asyncio.Semaphore
    ) -> list[ProviderServer]:
        """One domain's whole inventory, or `[]` if that domain failed.

        Failure is contained here on purpose, mirroring
        `tools.run_collector._run_one_manager`: an unreachable or slow
        domain must not cost the fleet its entire run.
        """
        async with sem:
            provider = self._domain_provider_factory(target)
            collected: list[ProviderServer] = []
            try:
                # Buffered rather than streamed through a queue. One
                # domain's servers is a small object list next to the
                # managed objects `UcsManagerProvider` already holds for
                # that same domain while it joins them, so a fan-in queue
                # would add machinery to save nothing.
                async with contextlib.aclosing(provider.list_servers()) as servers:
                    async for provider_server in servers:
                        collected.append(
                            replace(
                                provider_server,
                                external_id=central_external_id(
                                    provider_server.external_id, domain_id=target.domain_id
                                ),
                            )
                        )
            except Exception:
                logger.exception(
                    "ucs_central.domain_failed",
                    domain_id=target.domain_id,
                    domain_name=target.name,
                    endpoint=target.endpoint,
                    collected_before_failure=len(collected),
                )
                return []
            logger.info(
                "ucs_central.domain_collected",
                domain_id=target.domain_id,
                domain_name=target.name,
                endpoint=target.endpoint,
                servers=len(collected),
            )
            return collected

    async def list_servers(self) -> AsyncIterator[ProviderServer]:
        """Must be iterated to exhaustion (or closed via
        `contextlib.aclosing`) — abandoning this generator part-way leaves
        the per-domain sessions to be cleaned up at GC time, and both
        Central and UCS Manager enforce a per-user session cap.
        `IngestService.ingest` drains it fully.
        """
        targets, domain_mo_by_id = await self._plan()
        sem = asyncio.Semaphore(self._concurrency)
        per_domain = await asyncio.gather(
            *(self._collect_domain(target, sem) for target in targets)
        )

        collected_by_id = {
            target.domain_id: len(servers)
            for target, servers in zip(targets, per_domain, strict=True)
        }
        self._log_domains(domain_mo_by_id, collected_by_id=collected_by_id)

        for servers in per_domain:
            for provider_server in servers:
                yield provider_server

    def _log_domains(
        self, domain_mo_by_id: dict[str, Any], *, collected_by_id: dict[str, int]
    ) -> None:
        """Per-domain coverage, emitted every run.

        This is the check on Central's *domain list* — the one thing this
        collector still takes from the replica and cannot verify any other
        way. `total_physical_cnt` is what Central believes a domain holds;
        `collected_servers` is what that domain's own UCS Manager actually
        returned. Three failures show up in the gap between them, none of
        which is visible from the total ingested count:

        1. A domain Central lists but whose UCS Manager we could not
           collect from at all (`collected_servers=0` against a non-zero
           reported count) — an unreachable address, a login that is not
           valid on that domain, or a `domain_skipped` decision that
           pruned too hard.
        2. A domain whose registration Central has but whose inventory it
           never synced — `inventory_status` says so directly.
        3. Central's own view going stale: `last_refreshed_ts` is when
           Central last pulled from that domain, which is only ever a
           statement about the domain *list* here, never about the server
           data itself, since that comes straight from the domain.
        """
        for domain_id, mo in domain_mo_by_id.items():
            reported = getattr(mo, "total_physical_cnt", None)
            collected = collected_by_id.get(domain_id)
            logger.info(
                "ucs_central.domain_summary",
                domain_id=domain_id,
                domain_name=getattr(mo, "name", None),
                address=getattr(mo, "address", None),
                inventory_status=getattr(mo, "inventory_status", None),
                last_refreshed=getattr(mo, "last_refreshed_ts", None),
                reported_servers=reported,
                # `None` rather than 0 for a domain that was never
                # contacted, so "we asked and got nothing" and "we did not
                # ask" cannot be confused.
                collected_servers=collected,
            )
            if collected == 0 and str(reported or "0") not in ("0", ""):
                logger.warning(
                    "ucs_central.domain_collected_nothing",
                    domain_id=domain_id,
                    domain_name=getattr(mo, "name", None),
                    address=getattr(mo, "address", None),
                    reported_servers=reported,
                    hint=(
                        "UCS Central reports this domain holds servers, but its own UCS "
                        "Manager returned none. Check that the address is reachable and "
                        "that INVENTORY_UCS_MANAGER_USERNAME/_PASSWORD is valid on this "
                        "domain — see the ucs_central.domain_failed entry if there is one."
                    ),
                )
