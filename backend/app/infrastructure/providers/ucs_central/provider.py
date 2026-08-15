"""`ServerInventoryProvider` for Cisco UCS Central — every registered UCS
Manager domain in one collector run.

Why this exists alongside `..ucs_manager`, which already works: a UCS
Manager connection reaches exactly one domain, and
`EnvConnectionResolver` supplies exactly one endpoint per `ManagerType`,
so a fleet spread over several domains was unreachable past the first.
UCS Central is the aggregator Cisco ships for precisely that, and it
needs one endpoint and one login no matter how many domains register with
it.

## What was verified, and how

Cisco's UCS Central documentation is thin, so everything here was
confirmed against the SDK itself — the installed `ucscsdk==0.9.0.10`,
which is byte-identical to github.com/CiscoUcs/ucscsdk master at
`6c9a34f` ("Updated version for schema 2.1(1c)", 2025-08-26):

  - **DN shape.** `docs/ucscsdk_ug.rst` states it outright: a chassis in
    domain 1009 is `compute/sys-1009/chassis-1`, composed of
    `computeResourceAggrEp` -> `computeSystem` (`rn="sys-[domainId]"`) ->
    `equipmentChassis`. The mometa agrees: `ComputeSystem.mo_meta.rn` is
    `sys-[id]` with parent `computeResourceAggrEp`, and
    `ComputeBlade`/`ComputeRackUnit` both parent to `computeSystem`. So
    one domain-wide query spans every domain and the DN says which domain
    each object came from.
  - **Property parity with `ucsmsdk`.** Every attribute the mapping reads
    exists under the same name in `ucscsdk.mometa.*` — see
    `app.infrastructure.providers.ucs_common`, which is why that module
    and `..ucs_manager.mapping` are shared rather than reimplemented.
  - **Where a domain's address lives.** `ComputeSystem.address`, not
    `extpolClient` — confirmed by Cisco's own
    `ucscsdk/utils/ucscdomain.py`, whose `get_domain()` filters
    `ComputeSystem` on `(address, ..., type="eq")`. `extpolClient`
    (`extpol/reg/clients/client-<id>`) carries the registration state
    that the same file's `_is_domain_available()` checks for
    `oper_state == "registered"`.

## What could not be verified without a live UCS Central

**Whether Central's `lsServer` includes domain-*local* service profiles,
or only the global ones Central itself owns.** This is the one thing that
decides whether this collector works, because a UCS server's name comes
from its service profile — `computeBlade.name` is empty in practice
(`docs/adr/0009`), and the name is what carries the site token, the
classification pattern and the `INVENTORY_COLLECTOR_NAME_PATTERN` match.
`LsServer.mo_meta.parents` is `['computeTemplate', 'orgOrg']` in
`ucscsdk` — an org tree, with no `compute/sys-<id>` path — and Cisco's
SDK blurb describes the package as managing "global service profiles".

Rather than guess, `list_servers` counts profile coverage per domain and
logs it every run (`ucs_central.domain_summary`, and a loud
`ucs_central.domain_without_profiles` warning). If Central turns out not
to replicate local profiles, the symptom is a named, per-domain log line
saying so — not an inventory that silently comes back empty because every
server was named after its chassis slot and failed the `^ocp` filter.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import structlog

from app.domain.enums import ManagerType
from app.domain.models.manager import Manager
from app.domain.ports.credentials import ManagerConnection
from app.domain.ports.provider import ProviderServer
from app.infrastructure.providers.ucs_central.client import UcsCentralClient
from app.infrastructure.providers.ucs_common import (
    bmc_interface,
    group_by_owning_server_dn,
    is_equipped,
    partition_profiles,
)
from app.infrastructure.providers.ucs_manager.mapping import compute_unit_to_provider_server

logger = structlog.get_logger(__name__)

_PROVIDER_TYPE = ManagerType.UCS_CENTRAL.value


def domain_id_from_dn(dn: str) -> str | None:
    """The registered domain a `compute/sys-<domainId>/...` DN belongs to.

    Returns `None` for anything not under a `computeSystem`, which is how
    global objects (an org's service profiles, for one) are told apart
    from per-domain inventory.
    """
    parts = str(dn).split("/")
    if len(parts) < 2 or not parts[1].startswith("sys-"):
        return None
    return parts[1][len("sys-") :] or None


class UcsCentralProvider:
    provider_type = _PROVIDER_TYPE

    def __init__(
        self, *, manager: Manager, credentials: ManagerConnection, timeout_seconds: float
    ) -> None:
        if not manager.endpoint:
            raise ValueError(f"Manager {manager.id!r} has no endpoint configured.")
        self._endpoint: str = manager.endpoint
        self._manager = manager
        self._timeout_seconds = timeout_seconds
        self._credentials = credentials

    def _new_client(self) -> UcsCentralClient:
        return UcsCentralClient(
            endpoint=self._endpoint,
            username=self._credentials.username,
            password=self._credentials.password,
            timeout_seconds=self._timeout_seconds,
        )

    async def health_check(self) -> None:
        client = self._new_client()
        try:
            await client.login()
        finally:
            await client.logout()

    async def list_servers(self) -> AsyncIterator[ProviderServer]:
        """Must be iterated to exhaustion (or closed via
        `contextlib.aclosing`) — abandoning this generator part-way leaves
        the `finally` to run at GC time, and Central enforces a per-user
        session cap. `IngestService.ingest` drains it fully.
        """
        client = self._new_client()
        # `login()` belongs inside the try: the session is established
        # server-side before the SDK's post-login probes run, so a failure
        # in one of those would leak the session if login sat outside.
        try:
            await client.login()

            # Seven domain-wide queries for the entire multi-domain fleet.
            # The cost is per-class, not per-domain and not per-server:
            # this is the same seven calls whether Central fronts two
            # domains or two hundred.
            domains = await client.query_classid("computeSystem")
            blades = await client.query_classid("computeBlade")
            rack_units = await client.query_classid("computeRackUnit")
            ls_servers = await client.query_classid("lsServer")
            mgmt_ifs = await client.query_classid("mgmtIf")
            # Complementary, not alternatives — `adaptorExtEthIf` is the
            # physical adapter port, `adaptorHostEthIf` the logical vNIC
            # that only exists once a profile is associated. UCSPE 4.2
            # showed most servers have only one or the other, so querying
            # either alone leaves much of the fleet with no MACs and no
            # fabric attachments (`docs/adr/0009`).
            adapter_ifs_all = await client.query_classid("adaptorExtEthIf")
            adapter_ifs_all += await client.query_classid("adaptorHostEthIf")

            profile_by_dn, template_dn_by_name = partition_profiles(ls_servers)

            servers = [mo for mo in (*blades, *rack_units) if is_equipped(mo)]
            server_dns = [mo.dn for mo in servers]
            mgmt_ifs_by_server = group_by_owning_server_dn(mgmt_ifs, server_dns=server_dns)
            adapter_ifs_by_server = group_by_owning_server_dn(
                adapter_ifs_all, server_dns=server_dns
            )

            self._log_domains(domains, servers=servers, profile_by_dn=profile_by_dn)

            for server_mo in servers:
                mgmt_if = bmc_interface(mgmt_ifs_by_server[server_mo.dn], server_dn=server_mo.dn)
                yield compute_unit_to_provider_server(
                    server_mo,
                    manager_id=self._manager.id,
                    profile_by_dn=profile_by_dn,
                    template_dn_by_name=template_dn_by_name,
                    mgmt_if=mgmt_if,
                    adapter_ifs=adapter_ifs_by_server[server_mo.dn],
                    provider_type=_PROVIDER_TYPE,
                )
        finally:
            await client.logout()

    def _log_domains(
        self,
        domains: list[Any],
        *,
        servers: list[Any],
        profile_by_dn: dict[str, Any],
    ) -> None:
        """Per-domain coverage, emitted every run.

        Three distinct silent failures become visible here, and none of
        them is detectable from the ingested server count alone:

        1. A domain Central knows about but whose inventory it never
           collected — `inventory_status`/`total_physical_cnt` disagree
           with what we actually saw.
        2. A domain whose servers all resolve *no* service profile, which
           is the signature of Central not replicating domain-local
           profiles (see the module docstring). Those servers would fall
           back to a chassis-slot name and be silently dropped by
           `INVENTORY_COLLECTOR_NAME_PATTERN`.
        3. Stale replication — `last_refreshed_ts` is Central's own record
           of when it last pulled from that domain, which a direct UCS
           Manager collector never has to think about.
        """
        collected: dict[str, int] = {}
        with_profile: dict[str, int] = {}
        for mo in servers:
            did = domain_id_from_dn(mo.dn)
            if did is None:
                continue
            collected[did] = collected.get(did, 0) + 1
            if profile_by_dn.get(getattr(mo, "assigned_to_dn", None) or ""):
                with_profile[did] = with_profile.get(did, 0) + 1

        for domain in domains:
            did = str(getattr(domain, "id", "") or "")
            seen = collected.get(did, 0)
            named = with_profile.get(did, 0)
            logger.info(
                "ucs_central.domain_summary",
                domain_id=did,
                domain_name=getattr(domain, "name", None),
                address=getattr(domain, "address", None),
                inventory_status=getattr(domain, "inventory_status", None),
                last_refreshed=getattr(domain, "last_refreshed_ts", None),
                reported_servers=getattr(domain, "total_physical_cnt", None),
                collected_servers=seen,
                servers_with_profile=named,
            )
            if seen and not named:
                logger.warning(
                    "ucs_central.domain_without_profiles",
                    domain_id=did,
                    domain_name=getattr(domain, "name", None),
                    collected_servers=seen,
                    hint=(
                        "No server in this domain resolved a service profile, so every one of "
                        "them falls back to a chassis-slot name that carries no site token and "
                        "will not match INVENTORY_COLLECTOR_NAME_PATTERN. Most likely UCS "
                        "Central does not replicate this domain's locally-defined service "
                        "profiles — collect this domain via its own UCS Manager instead."
                    ),
                )

        # A domain holding servers but absent from `computeSystem` would
        # otherwise be invisible in the loop above.
        unknown = set(collected) - {str(getattr(d, "id", "") or "") for d in domains}
        if unknown:
            logger.warning(
                "ucs_central.servers_in_unlisted_domain",
                domain_ids=sorted(unknown),
                servers=sum(collected[d] for d in unknown),
            )
