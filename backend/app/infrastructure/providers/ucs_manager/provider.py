"""`ServerInventoryProvider` for a single Cisco UCS Manager domain.

See docs/cisco-collectors.md, "Shared object model and DN joins".
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
    """
    Collects one UCS Manager domain's inventory.

    One instance is scoped to one domain. The UCS Central collector builds
    one per registered domain and runs each independently, so a slow or
    unreachable domain never blocks the others.

    See docs/cisco-collectors.md, "Shared object model and DN joins".
    """

    provider_type = ManagerType.UCS_MANAGER.value

    def __init__(
        self, *, manager: Manager, credentials: ManagerConnection, timeout_seconds: float
    ) -> None:
        """
        Bind a provider to one UCS Manager domain.

        Args:
            manager (Manager): The manager this run reports under. Its `id`
                becomes each server's `manager_id`, its `endpoint` is the
                domain to connect to.
            credentials (ManagerConnection): Login for the domain.
            timeout_seconds (float): Per-socket-operation timeout.

        Raises:
            ValueError: If `manager` has no endpoint configured.
        """
        if not manager.endpoint:
            raise ValueError(f"Manager {manager.id!r} has no endpoint configured.")
        self._endpoint: str = manager.endpoint
        self._manager = manager
        self._timeout_seconds = timeout_seconds
        self._credentials = credentials

    def _new_client(self) -> UcsManagerClient:
        """
        Build a client, and so a login session, for this domain.

        Returns:
            UcsManagerClient: A fresh client. Sessions are never shared or
                reused across calls.
        """
        return UcsManagerClient(
            endpoint=self._endpoint,
            username=self._credentials.username,
            password=self._credentials.password,
            timeout_seconds=self._timeout_seconds,
        )

    async def health_check(self) -> None:
        """
        Verify the domain is reachable and the credentials are accepted.

        Raises:
            UcsManagerConnectionError: If login fails for any reason.
        """
        client = self._new_client()
        try:
            await client.login()
        finally:
            await client.logout()

    async def list_servers(self) -> AsyncIterator[ProviderServer]:
        """
        Yield every physically-present server in the domain.

        Must be iterated to exhaustion, or closed via
        `contextlib.aclosing`: abandoning it part-way defers the session
        teardown to GC time, and UCS Manager enforces a per-user session
        cap. `IngestService.ingest` drains it fully.

        Yields:
            ProviderServer: One equipped compute unit, already normalized.

        Raises:
            UcsManagerConnectionError: On login failure or any query
                failure.

        See docs/cisco-collectors.md, "Shared object model and DN joins"
        and "Adapter interfaces, MACs and fabric attachments".
        """
        client = self._new_client()
        try:
            await client.login()

            blades = await client.query_classid("computeBlade")
            rack_units = await client.query_classid("computeRackUnit")
            ls_servers = await client.query_classid("lsServer")
            mgmt_ifs = await client.query_classid("mgmtIf")
            ext_eth_ifs_all = await client.query_classid("adaptorExtEthIf")
            host_eth_ifs_all = await client.query_classid("adaptorHostEthIf")
            cpu_units_all = await client.query_classid("processorUnit")
            disk_units_all = await client.query_classid("storageLocalDisk")

            profile_by_dn, template_dn_by_name = _partition_profiles(ls_servers)

            servers = [mo for mo in (*blades, *rack_units) if _is_equipped(mo)]
            server_dns = [mo.dn for mo in servers]
            mgmt_ifs_by_server = _group_by_owning_server_dn(mgmt_ifs, server_dns=server_dns)
            ext_eth_ifs_by_server = _group_by_owning_server_dn(
                ext_eth_ifs_all, server_dns=server_dns
            )
            host_eth_ifs_by_server = _group_by_owning_server_dn(
                host_eth_ifs_all, server_dns=server_dns
            )
            cpu_units_by_server = _group_by_owning_server_dn(cpu_units_all, server_dns=server_dns)
            disk_units_by_server = _group_by_owning_server_dn(disk_units_all, server_dns=server_dns)

            for server_mo in servers:
                mgmt_if = _bmc_interface(mgmt_ifs_by_server[server_mo.dn], server_dn=server_mo.dn)
                yield compute_unit_to_provider_server(
                    server_mo,
                    manager_id=self._manager.id,
                    profile_by_dn=profile_by_dn,
                    template_dn_by_name=template_dn_by_name,
                    mgmt_if=mgmt_if,
                    ext_eth_ifs=ext_eth_ifs_by_server[server_mo.dn],
                    host_eth_ifs=host_eth_ifs_by_server[server_mo.dn],
                    cpu_units=cpu_units_by_server[server_mo.dn],
                    disk_units=disk_units_by_server[server_mo.dn],
                )
        finally:
            await client.logout()
