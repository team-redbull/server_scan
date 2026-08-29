"""`ServerInventoryProvider` for Cisco Intersight.

The first collector whose cost does not scale with the fleet. Every
sub-resource carries a reference back to its owner, so each is listed
once for the whole estate and joined in memory against
`compute.PhysicalSummary` — on the order of a hundred requests for
10,000 servers, against the Redfish collector's ~25 per BMC. See
docs/adr/0017-intersight-collector.md, "The request plan".

Servers managed by UCS Manager are deliberately not collected here: they
are exactly the set `..ucs_central` already owns, and collecting both
would make one document's fields flip on every run. ADR-0017,
"Decision 3".
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from typing import Any

import structlog

from app.domain.enums import ManagerType
from app.domain.models.manager import Manager
from app.domain.ports.credentials import ManagerConnection
from app.domain.ports.provider import ProviderServer
from app.infrastructure.providers.intersight import mapping
from app.infrastructure.providers.intersight.client import (
    IntersightClient,
    IntersightError,
)

logger = structlog.get_logger(__name__)

_PROVIDER_TYPE = ManagerType.INTERSIGHT.value

# `$select` per query, so the join tables hold only mapped fields. This
# is the setting that decides how much memory a 10,000-server run needs,
# not a micro-optimisation.
_SUMMARY_FIELDS = (
    "Moid,Dn,Name,UserLabel,Model,Serial,Uuid,Vendor,TotalMemory,NumCpus,NumCpuCores,"
    "NumThreads,MgmtIpAddress,ManagementMode,ServiceProfile"
)
_PROFILE_FIELDS = "Moid,Name,Dn,AssignedServer,SrcTemplate"
_TEMPLATE_FIELDS = "Moid,Name"
_ADAPTER_UNIT_FIELDS = "Moid,ComputeBlade,ComputeRackUnit"
_EXT_IF_FIELDS = (
    "Moid,AdapterUnit,SwitchId,MacAddress,ExtEthInterfaceId,AdminState,OperState,PeerDn,PeerPortId"
)
_HOST_IF_FIELDS = "Moid,AdapterUnit,Name,HostEthInterfaceId,MacAddress,AdminState,OperState,PeerDn"
_STORAGE_CONTROLLER_FIELDS = "Moid,ComputeBlade,ComputeRackUnit"
_DISK_FIELDS = (
    "Moid,DiskId,Model,Pid,Serial,Type,Size,NonCoercedSizeBytes,Health,DriveState,"
    "FailurePredicted,StorageController"
)
_CARD_FIELDS = "Moid,Model,Pid,Vendor,Serial,OperState,ComputeBlade,ComputeRackUnit"
_MGMT_CONTROLLER_FIELDS = "Moid,ComputeBlade,ComputeRackUnit"
_MGMT_INTERFACE_FIELDS = "Moid,MacAddress,IpAddress,Ipv4Address,ManagementController"


def _owning_server(mo: Mapping[str, Any]) -> str | None:
    """
    The compute `Moid` a directly-attached object belongs to.

    Args:
        mo (Mapping[str, Any]): Any MO carrying `ComputeBlade` and
            `ComputeRackUnit` relationships — exactly one is set,
            depending on whether the server is a blade or a rack unit.

    Returns:
        str | None: The owning server's `Moid`, or None when neither is
            set (a spare part, or an object owned by a chassis).
    """
    return mapping.moref(mo.get("ComputeBlade")) or mapping.moref(mo.get("ComputeRackUnit"))


def _group_by(
    rows: Iterable[Mapping[str, Any]], key: Callable[[Mapping[str, Any]], str | None]
) -> dict[str, list[Mapping[str, Any]]]:
    """
    Bucket rows by a derived key, dropping rows the key cannot resolve.

    Args:
        rows (Iterable[Mapping[str, Any]]): The MOs to group.
        key (Callable[[Mapping[str, Any]], str | None]): Extracts the
            grouping key, returning None for a row to drop.

    Returns:
        dict[str, list[Mapping[str, Any]]]: Rows by key, order preserved.
    """
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        resolved = key(row)
        if resolved:
            grouped.setdefault(resolved, []).append(row)
    return grouped


class _Joins:
    """
    Every sub-resource table one run collected, keyed by server `Moid`.

    A table is `None` when its query failed, which is carried all the way
    to `ProviderServer` so `IngestService` preserves the stored value
    instead of overwriting it with an empty one. That distinction is the
    whole reason this is a class and not a dict of lists.
    """

    def __init__(self) -> None:
        """Start with every table unread."""
        self.profiles: dict[str, Mapping[str, Any]] | None = None
        self.templates: dict[str, Mapping[str, Any]] = {}
        self.ext_interfaces: dict[str, list[Mapping[str, Any]]] | None = None
        self.host_interfaces: dict[str, list[Mapping[str, Any]]] | None = None
        self.disks: dict[str, list[Mapping[str, Any]]] | None = None
        self.cards: dict[str, list[Mapping[str, Any]]] | None = None
        self.management: dict[str, Mapping[str, Any]] | None = None

    def for_server(self, moid: str, table: dict[str, list[Mapping[str, Any]]] | None):  # type: ignore[no-untyped-def]
        """
        One server's rows from a table, preserving the unread distinction.

        Args:
            moid (str): The server's `Moid`.
            table (dict[str, list[Mapping[str, Any]]] | None): The table.

        Returns:
            list[Mapping[str, Any]] | None: Its rows, `[]` when the table
                was read and holds none for this server, or None when the
                table was never read.
        """
        if table is None:
            return None
        return table.get(moid, [])


class IntersightProvider:
    """
    Collect an Intersight tenant or on-prem appliance in one pass.

    See docs/adr/0017-intersight-collector.md.
    """

    provider_type = _PROVIDER_TYPE

    def __init__(
        self,
        *,
        manager: Manager,
        credentials: ManagerConnection,
        connect_timeout: float = 10.0,
        read_timeout: float = 60.0,
        ca_bundle: str | None = None,
        page_size: int = 1000,
        management_modes: tuple[str, ...] = (mapping.MODE_IMM, mapping.MODE_STANDALONE),
        run_budget_seconds: float = 1800.0,
        debug_http: bool = False,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        """
        Args:
            manager (Manager): The `Manager` projection this run writes.
            credentials (ManagerConnection): `endpoint` is the host,
                `username` the API Key ID and `password` the key's PEM —
                Intersight signs requests rather than logging in.
            connect_timeout (float): Seconds to establish a connection.
            read_timeout (float): Seconds to wait for one page.
            ca_bundle (str | None): Extra trusted PEM bundle, for an
                on-prem appliance with an internal CA.
            page_size (int): `$top`, capped at the API's 1000.
            management_modes (tuple[str, ...]): Which `ManagementMode`
                values to collect. Excluding `UCSM` is what keeps this
                collector and the UCS Central one from fighting over the
                same servers.
            run_budget_seconds (float): Wall clock for the whole run,
                enforced in-process so a throttled run ends with a
                summary rather than being killed with none.
            debug_http (bool): Log method, path and status per request.
            client_factory (Callable[[], Any] | None): Injected in tests.
        """
        self._manager = manager
        self._credentials = credentials
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._ca_bundle = ca_bundle
        self._page_size = page_size
        self._modes = tuple(management_modes)
        self._run_budget_seconds = run_budget_seconds
        self._debug_http = debug_http
        self._client_factory = client_factory
        self._collection_errors: list[str] = []

    @property
    def collection_errors(self) -> tuple[str, ...]:
        """
        Sub-resource queries this run could not complete.

        Read by `tools.run_collector`, which turns a non-empty result
        into exit code 3 (PARTIAL). A run that could not read drives is
        not a run that found no drives, and must not be reported as a
        clean one.

        Returns:
            tuple[str, ...]: One message per failed query.
        """
        return tuple(self._collection_errors)

    def _new_client(self) -> Any:
        """
        Build the client this run talks through.

        Returns:
            Any: An `IntersightClient`, or whatever a test injected.
        """
        if self._client_factory is not None:
            return self._client_factory()
        return IntersightClient(
            endpoint=self._credentials.endpoint,
            key_id=self._credentials.username,
            private_key_pem=self._credentials.password,
            connect_timeout=self._connect_timeout,
            read_timeout=self._read_timeout,
            ca_bundle=self._ca_bundle,
            page_size=self._page_size,
            debug_http=self._debug_http,
        )

    async def health_check(self) -> None:
        """
        Prove the endpoint answers and the API key is accepted.

        Raises:
            IntersightError: With a message naming what to fix.
        """
        client = self._new_client()
        try:
            await client.health_check()
        finally:
            await client.aclose()

    def _mode_filter(self) -> str | None:
        """
        The `$filter` restricting the run to the modes we collect.

        Pushed server-side because it shrinks the anchor result set
        itself. Unlike a name filter it is also *correct* to push: the
        mode is a field on the summary, whereas a server's real name is
        not (see `mapping.server_name`).

        Returns:
            str | None: An OData expression, or None to collect all modes.
        """
        if not self._modes:
            return None
        return " or ".join(f"ManagementMode eq '{mode}'" for mode in self._modes)

    async def _collect_table(
        self,
        client: Any,
        resource: str,
        *,
        select: str,
    ) -> list[Mapping[str, Any]] | None:
        """
        Read one sub-resource fleet-wide, tolerating its failure.

        Args:
            client (Any): The Intersight client.
            resource (str): Path under `/api/v1`.
            select (str): `$select` field list.

        Returns:
            list[Mapping[str, Any]] | None: Every row, or None if the
                query failed — which is recorded and reported, never
                turned into an empty list.
        """
        try:
            return [row async for row in client.list_all(resource, select=select)]
        except IntersightError as exc:
            message = f"{resource}: {exc}"
            self._collection_errors.append(message)
            logger.warning("intersight.subresource_failed", resource=resource, error=str(exc))
            return None

    async def _build_joins(self, client: Any) -> _Joins:
        """
        Read every sub-resource once and index it by owning server.

        Args:
            client (Any): The Intersight client.

        Returns:
            _Joins: The indexed tables, with `None` for any that failed.
        """
        joins = _Joins()

        profiles = await self._collect_table(client, "server/Profiles", select=_PROFILE_FIELDS)
        if profiles is not None:
            joins.profiles = {
                server: rows[0]
                for server, rows in _group_by(
                    profiles, lambda p: mapping.moref(p.get("AssignedServer"))
                ).items()
            }
            templates = await self._collect_table(
                client, "server/ProfileTemplates", select=_TEMPLATE_FIELDS
            )
            joins.templates = {
                str(t.get("Moid")): t for t in templates or () if t.get("Moid") is not None
            }

        adapter_units = await self._collect_table(
            client, "adapter/Units", select=_ADAPTER_UNIT_FIELDS
        )
        if adapter_units is not None:
            server_by_adapter = {
                str(unit.get("Moid")): _owning_server(unit)
                for unit in adapter_units
                if unit.get("Moid") is not None
            }

            def by_adapter(interface: Mapping[str, Any]) -> str | None:
                """
                Resolve an interface to its server through its adapter.

                Args:
                    interface (Mapping[str, Any]): An adapter interface.

                Returns:
                    str | None: The owning server's `Moid`, or None.
                """
                unit = mapping.moref(interface.get("AdapterUnit"))
                return server_by_adapter.get(unit) if unit else None

            ext = await self._collect_table(
                client, "adapter/ExtEthInterfaces", select=_EXT_IF_FIELDS
            )
            if ext is not None:
                joins.ext_interfaces = _group_by(ext, by_adapter)
            host = await self._collect_table(
                client, "adapter/HostEthInterfaces", select=_HOST_IF_FIELDS
            )
            if host is not None:
                joins.host_interfaces = _group_by(host, by_adapter)

        controllers = await self._collect_table(
            client, "storage/Controllers", select=_STORAGE_CONTROLLER_FIELDS
        )
        if controllers is not None:
            server_by_controller = {
                str(c.get("Moid")): _owning_server(c)
                for c in controllers
                if c.get("Moid") is not None
            }
            disks = await self._collect_table(client, "storage/PhysicalDisks", select=_DISK_FIELDS)
            if disks is not None:
                joins.disks = _group_by(
                    disks,
                    lambda d: server_by_controller.get(
                        mapping.moref(d.get("StorageController")) or ""
                    ),
                )

        cards = await self._collect_table(client, "graphics/Cards", select=_CARD_FIELDS)
        if cards is not None:
            joins.cards = _group_by(cards, _owning_server)

        mgmt_controllers = await self._collect_table(
            client, "management/Controllers", select=_MGMT_CONTROLLER_FIELDS
        )
        if mgmt_controllers is not None:
            server_by_controller = {
                str(c.get("Moid")): _owning_server(c)
                for c in mgmt_controllers
                if c.get("Moid") is not None
            }
            interfaces = await self._collect_table(
                client, "management/Interfaces", select=_MGMT_INTERFACE_FIELDS
            )
            if interfaces is not None:
                joins.management = {
                    server: rows[0]
                    for server, rows in _group_by(
                        interfaces,
                        lambda i: server_by_controller.get(
                            mapping.moref(i.get("ManagementController")) or ""
                        ),
                    ).items()
                }

        return joins

    async def list_servers(self) -> AsyncIterator[ProviderServer]:
        """
        Every server this endpoint reports, in the modes configured.

        The sub-resource tables are read first and held for the length of
        the run; the servers themselves are streamed, so the collector
        never materialises the fleet.

        Yields:
            ProviderServer: One server, fully joined.

        Raises:
            IntersightError: If the server list itself could not be read.
        """
        started = time.monotonic()
        client = self._new_client()
        try:
            joins = await self._build_joins(client)

            collected = 0
            async for summary in client.list_all(
                "compute/PhysicalSummaries",
                select=_SUMMARY_FIELDS,
                filter_expr=self._mode_filter(),
            ):
                elapsed = time.monotonic() - started
                if elapsed > self._run_budget_seconds:
                    message = (
                        f"run budget of {self._run_budget_seconds:.0f}s exhausted after "
                        f"{collected} server(s); the rest of the fleet was not read"
                    )
                    self._collection_errors.append(message)
                    logger.warning("intersight.run_budget_exhausted", collected=collected)
                    break

                moid = str(summary.get("Moid") or "")
                if not moid:
                    continue
                profile = joins.profiles.get(moid) if joins.profiles is not None else None
                template = None
                if profile is not None:
                    template_moid = mapping.moref(profile.get("SrcTemplate"))
                    template = joins.templates.get(template_moid) if template_moid else None

                collected += 1
                yield mapping.to_provider_server(
                    summary,
                    provider_type=self.provider_type,
                    manager_id=self._manager.id,
                    profile=profile,
                    template=template,
                    ext_interfaces=joins.for_server(moid, joins.ext_interfaces),
                    host_interfaces=joins.for_server(moid, joins.host_interfaces),
                    disks=joins.for_server(moid, joins.disks),
                    cards=joins.for_server(moid, joins.cards),
                    management_interface=(
                        joins.management.get(moid) if joins.management is not None else None
                    ),
                )

            logger.info(
                "intersight.run_summary",
                collected=collected,
                modes=list(self._modes),
                seconds=round(time.monotonic() - started, 1),
                unreadable_subresources=len(self._collection_errors),
            )
        finally:
            await client.aclose()
