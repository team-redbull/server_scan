"""`ServerInventoryProvider` implementation for a single Cisco UCS Manager
domain — the real-collector counterpart to
`app.infrastructure.providers.fake.provider.FakeProvider`, satisfying the
exact same `app.domain.ports.provider.ServerInventoryProvider` Protocol so
`app.application.services.ingest.IngestService` doesn't need to know or
care whether it's ingesting fake or real data.

One instance is scoped to one UCS Manager domain (one `Manager` document,
`type=UCS_MANAGER`) — `tools.run_collector` constructs one provider per
manager and runs each through its own `IngestService.ingest()` call, so a
failure or slow domain never blocks the others.

Every lookup here is a *domain-wide* `query_classid` joined client-side by
distinguished name, never a per-server `query_children`. That's a
correctness requirement before it's a performance one: `mgmtIf` and
`adaptorHostEthIf` are both **grandchildren** of a compute unit, not
children of one, so a `configResolveChildren` scoped to a blade's DN and
filtered to either class matches nothing at all. Confirmed against the
installed `ucsmsdk==0.9.27` MO metadata:

    adaptorHostEthIf  parents=['adaptorUnit']                       rn=host-eth-[id]
    adaptorUnit       parents=['computeBlade','computeRackUnit',...] rn=adaptor-[id]
    mgmtIf            parents=['adaptorHostEthIf','mgmtController']  rn=if-[id]
    mgmtController    parents=[...,'computeBlade','computeRackUnit'] rn=mgmt

so the real DNs are `sys/chassis-1/blade-1/adaptor-1/host-eth-1` and
`sys/chassis-1/blade-1/mgmt/if-1` — two levels down. `hierarchy=True` does
not widen the class filter's depth; it only asks the server to attach each
*matched* object's subtree, which `ucsmsdk.ucscoreutils.
extract_molist_from_method_response` then flattens with no class filter at
all (so a match would return foreign MO classes mixed into the list).
Cisco's own SDK agrees: its blade -> mgmtIf lookup in `ucsmsdk/utils/
ucskvmlaunch.py` uses `configScope`, not `configResolveChildren`, and
`ucsmsdk/utils/inventory.py` collects adapters with a domain-wide
`query_classid`.

Joining on a DN prefix is exact rather than heuristic: `ucsmo.py` builds
every MO's `dn` as `parent_dn + "/" + rn`, so a descendant's DN always
starts with its owning server's DN followed by a separator.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from app.domain.enums import ManagerType
from app.domain.models.manager import Manager
from app.domain.ports.credentials import ManagerConnection
from app.domain.ports.provider import ProviderServer
from app.infrastructure.providers.ucs_common import (
    bmc_interface as _bmc_interface,
)
from app.infrastructure.providers.ucs_common import (
    group_by_owning_server_dn as _group_by_owning_server_dn,
)
from app.infrastructure.providers.ucs_common import (
    is_equipped as _is_equipped,
)
from app.infrastructure.providers.ucs_common import (
    partition_profiles as _partition_profiles,
)
from app.infrastructure.providers.ucs_manager.client import UcsManagerClient
from app.infrastructure.providers.ucs_manager.mapping import compute_unit_to_provider_server


class UcsManagerProvider:
    provider_type = ManagerType.UCS_MANAGER.value

    def __init__(
        self, *, manager: Manager, credentials: ManagerConnection, timeout_seconds: float
    ) -> None:
        if not manager.endpoint:
            raise ValueError(f"Manager {manager.id!r} has no endpoint configured.")
        # Narrowed to `str` here (once, where the check actually lives) so
        # `_new_client` doesn't need to re-prove `endpoint` is non-`None`
        # to the type checker on every call.
        self._endpoint: str = manager.endpoint
        self._manager = manager
        self._timeout_seconds = timeout_seconds
        self._credentials = credentials

    def _new_client(self) -> UcsManagerClient:
        # A fresh `UcsManagerClient` (and so a fresh login session) per
        # call — see `UcsManagerClient`'s docstring on why sessions aren't
        # shared/reused across calls.
        return UcsManagerClient(
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
        """Must be iterated to exhaustion (or explicitly closed via
        `contextlib.aclosing`) — abandoning this generator part-way leaves
        the `finally` below to run at GC time, and UCS Manager enforces a
        per-user session cap. `IngestService.ingest` drains it fully.
        """
        client = self._new_client()
        # `login()` belongs *inside* the try: `ucssession._login` sets the
        # session cookie and only then calls `_update_version()` /
        # `_update_domain_name_and_ip()`, either of which can raise with
        # the session already established server-side. Logging in outside
        # the try would leak that session until UCS Manager times it out.
        try:
            await client.login()

            blades = await client.query_classid("computeBlade")
            rack_units = await client.query_classid("computeRackUnit")
            ls_servers = await client.query_classid("lsServer")
            mgmt_ifs = await client.query_classid("mgmtIf")
            # Both adapter interface classes, unioned per server. They are
            # complementary, not alternatives: `adaptorExtEthIf` is the
            # physical adapter port (present on every discovered server,
            # burned-in MAC, cabled to a fabric interconnect), while
            # `adaptorHostEthIf` is a logical vNIC that only exists once a
            # service profile is associated. Verified against UCSPE 4.2:
            # of 14 servers, 12 had only ext-eth ports and 2 had only
            # host-eth — querying either class alone left most of the
            # fleet with no MACs and no fabric attachments at all.
            adapter_ifs_all = await client.query_classid("adaptorExtEthIf")
            adapter_ifs_all += await client.query_classid("adaptorHostEthIf")

            profile_by_dn, template_dn_by_name = _partition_profiles(ls_servers)

            servers = [mo for mo in (*blades, *rack_units) if _is_equipped(mo)]
            server_dns = [mo.dn for mo in servers]
            mgmt_ifs_by_server = _group_by_owning_server_dn(mgmt_ifs, server_dns=server_dns)
            adapter_ifs_by_server = _group_by_owning_server_dn(
                adapter_ifs_all, server_dns=server_dns
            )

            for server_mo in servers:
                mgmt_if = _bmc_interface(mgmt_ifs_by_server[server_mo.dn], server_dn=server_mo.dn)
                yield compute_unit_to_provider_server(
                    server_mo,
                    manager_id=self._manager.id,
                    profile_by_dn=profile_by_dn,
                    template_dn_by_name=template_dn_by_name,
                    mgmt_if=mgmt_if,
                    adapter_ifs=adapter_ifs_by_server[server_mo.dn],
                )
        finally:
            await client.logout()
