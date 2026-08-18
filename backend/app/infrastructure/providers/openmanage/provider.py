"""`ServerInventoryProvider` for Dell servers — the OpenManage entry point.

One OpenManage Enterprise (OME) appliance manages the whole Dell estate,
so a single provider instance covers the fleet: two bulk REST calls
enumerate every server profile and every managed device, and a bounded
fan-out of per-device inventory calls fills in the CPU, memory, storage and
NIC detail OME sources from each server's iDRAC.

This mirrors the platform's existing collector shape: the bulk calls are
the cheap "who is out there" pass (like UCS Central's domain discovery),
and the per-device inventory is the expensive per-machine pass. As with the
Cisco collector, a `name_pattern` prunes the expensive pass to this
platform's own fleet before any inventory call is spent — the authoritative
name filter still runs in `tools.run_collector`, this is only an
efficiency gate.

See docs/dell-collectors.md for the OME field/endpoint facts, carried over
from a validated production scanner.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Iterable
from typing import Any

import structlog

from app.domain.enums import ManagerType
from app.domain.models.manager import Manager
from app.domain.ports.credentials import ManagerConnection
from app.domain.ports.provider import ProviderServer
from app.infrastructure.providers.openmanage.client import OmeClient, OmeConnectionError
from app.infrastructure.providers.openmanage.mapping import to_provider_server

logger = structlog.get_logger(__name__)

_PROVIDER_TYPE = ManagerType.OPENMANAGE.value

_INVENTORY_SECTIONS = (
    "serverProcessors",
    "serverMemoryDevices",
    "serverStorage",
    "serverNetworkInterfaces",
)


class OpenManageProvider:
    """
    Collects one OpenManage Enterprise appliance's Dell inventory.

    One instance covers one OME appliance and every Dell server it manages.

    See docs/dell-collectors.md, "Collection flow".
    """

    provider_type = _PROVIDER_TYPE

    def __init__(
        self,
        *,
        manager: Manager,
        credentials: ManagerConnection,
        timeout_seconds: float,
        name_pattern: str = "",
        concurrency: int = 8,
        verify_tls: bool = False,
    ) -> None:
        """
        Bind a provider to one OME appliance.

        Args:
            manager (Manager): The manager this run reports under. Its `id`
                becomes each server's `manager_id`; its `endpoint` is the
                OME appliance to connect to.
            credentials (ManagerConnection): OME appliance login
                (`INVENTORY_OME_IP`/`_USERNAME`/`_PASSWORD`).
            timeout_seconds (float): Per-request timeout.
            name_pattern (str): Regex; profiles whose name does not match it
                are skipped before their inventory is fetched. Empty means
                inventory every profile. Only an efficiency gate — the
                authoritative filter is `tools.run_collector`'s wrapper.
            concurrency (int): How many devices to inventory at once.
            verify_tls (bool): Whether to verify the appliance's TLS
                certificate; defaults to `False` for self-signed appliances.

        Raises:
            ValueError: If `manager` has no endpoint configured.
        """
        if not manager.endpoint:
            raise ValueError(f"Manager {manager.id!r} has no endpoint configured.")
        self._endpoint: str = manager.endpoint
        self._manager = manager
        self._credentials = credentials
        self._timeout_seconds = timeout_seconds
        self._pattern = re.compile(name_pattern) if name_pattern else None
        self._concurrency = max(1, concurrency)
        self._verify_tls = verify_tls
        self._collection_errors: list[str] = []

    @property
    def collection_errors(self) -> tuple[str, ...]:
        """Devices whose inventory could not be fully read this run.

        Read by `tools.run_collector` so a run that reached OME but could
        not inventory every matched server reports PARTIAL rather than a
        silently-complete success — the same honesty the Cisco collector
        applies to an unreachable domain.
        """
        return tuple(self._collection_errors)

    def _new_client(self) -> OmeClient:
        """
        Build a client for this appliance. Sessions are never shared across
        calls.

        Returns:
            OmeClient: A fresh, not-yet-logged-in client.
        """
        return OmeClient(
            endpoint=self._endpoint,
            username=self._credentials.username,
            password=self._credentials.password,
            timeout_seconds=self._timeout_seconds,
            verify_tls=self._verify_tls,
        )

    async def health_check(self) -> None:
        """
        Verify the appliance is reachable and the credentials are accepted.

        Raises:
            OmeConnectionError: If login fails for any reason.
        """
        async with self._new_client():
            return

    async def list_servers(self) -> AsyncIterator[ProviderServer]:
        """
        Yield every Dell server OME manages, enriched from its inventory.

        Two bulk calls enumerate profiles and devices; matching profiles are
        then inventoried a bounded batch at a time so one appliance is never
        hit with the whole fleet's per-device calls at once.

        Yields:
            ProviderServer: One managed Dell server, already normalized.

        Raises:
            OmeConnectionError: On login failure or a failure of either bulk
                enumeration call. A per-device inventory failure is recorded
                in `collection_errors` and does not abort the run.

        See docs/dell-collectors.md, "Collection flow".
        """
        self._collection_errors = []
        async with self._new_client() as client:
            profiles = await client.get_all("/ProfileService/Profiles")
            devices = await client.get_all("/DeviceService/Devices")
            device_by_ip = {
                str(device.get("DeviceName")): device
                for device in devices
                if device.get("DeviceName") is not None
            }
            logger.info(
                "ome.enumerated",
                endpoint=self._endpoint,
                profiles=len(profiles),
                devices=len(devices),
            )

            matching = [p for p in profiles if self._matches(p)]
            for batch in _chunked(matching, self._concurrency):
                for server in await self._inventory_batch(client, batch, device_by_ip):
                    yield server

    def _matches(self, profile: dict[str, Any]) -> bool:
        """
        Whether a profile's name passes the efficiency pre-filter.

        Args:
            profile (dict[str, Any]): One `/ProfileService/Profiles` entry.

        Returns:
            bool: `True` when there is no pattern or the profile's
                `ProfileName` matches it.
        """
        if self._pattern is None:
            return True
        return bool(self._pattern.search(str(profile.get("ProfileName") or "")))

    async def _inventory_batch(
        self,
        client: OmeClient,
        profiles: list[dict[str, Any]],
        device_by_ip: dict[str, dict[str, Any]],
    ) -> list[ProviderServer]:
        """
        Build `ProviderServer`s for one batch of profiles concurrently.

        Args:
            client (OmeClient): The logged-in OME client.
            profiles (list[dict[str, Any]]): The batch of profiles.
            device_by_ip (dict[str, dict[str, Any]]): iDRAC IP -> device,
                for joining each profile to its managed device.

        Returns:
            list[ProviderServer]: One entry per profile, in order.
        """
        results = await asyncio.gather(
            *(self._build_one(client, profile, device_by_ip) for profile in profiles)
        )
        return list(results)

    async def _build_one(
        self,
        client: OmeClient,
        profile: dict[str, Any],
        device_by_ip: dict[str, dict[str, Any]],
    ) -> ProviderServer:
        """
        Fetch one profile's device inventory and map it to a `ProviderServer`.

        A missing managed device, or an inventory section that fails to
        load, degrades to empty rather than dropping the server: identity
        from the profile is always worth ingesting even when detail is
        partial. Any such gap is recorded in `collection_errors`.

        Args:
            client (OmeClient): The logged-in OME client.
            profile (dict[str, Any]): One `/ProfileService/Profiles` entry.
            device_by_ip (dict[str, dict[str, Any]]): iDRAC IP -> device.

        Returns:
            ProviderServer: The normalized server DTO.
        """
        idrac_ip = str(profile.get("TargetName") or "")
        device = device_by_ip.get(idrac_ip, {})
        device_id = device.get("Id")

        sections: dict[str, list[dict[str, Any]]] = {name: [] for name in _INVENTORY_SECTIONS}
        if device_id is not None:
            for section in _INVENTORY_SECTIONS:
                try:
                    sections[section] = await client.get_inventory(device_id, section)
                except OmeConnectionError as exc:
                    message = (
                        f"{profile.get('ProfileName')!r}: {section} inventory unavailable ({exc})"
                    )
                    self._collection_errors.append(message)
                    logger.warning(
                        "ome.inventory_failed",
                        profile=profile.get("ProfileName"),
                        section=section,
                        error=str(exc),
                    )

        return to_provider_server(
            profile=profile,
            device=device,
            processors=sections["serverProcessors"],
            memory_modules=sections["serverMemoryDevices"],
            storage=sections["serverStorage"],
            network_interfaces=sections["serverNetworkInterfaces"],
            manager_id=self._manager.id,
        )


def _chunked(items: list[Any], size: int) -> Iterable[list[Any]]:
    """
    Yield successive `size`-length slices of `items`.

    Args:
        items (list[Any]): The list to slice.
        size (int): Slice length, at least 1.

    Yields:
        list[Any]: Each slice in order; the last may be shorter.
    """
    for start in range(0, len(items), size):
        yield items[start : start + size]
