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
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from app.domain.enums import ManagerType
from app.domain.models.manager import Manager
from app.domain.ports.credentials import ManagerCredentials
from app.domain.ports.provider import ProviderServer
from app.infrastructure.providers.ucs_manager.client import UcsManagerClient
from app.infrastructure.providers.ucs_manager.mapping import compute_unit_to_provider_server

# See `ComputeBladeConsts`/`ComputeRackUnitConsts` in the installed
# `ucsmsdk` package — every "equipped*" variant (including the
# unsupported/malformed/identity-unestablishable ones) is a real,
# physically-present server; only "empty"/"missing"/"unauthorized"/
# "mismatch"/"unknown"/"inaccessible" are slots with nothing usable to
# report.
_EQUIPPED_PREFIX = "equipped"


def _is_equipped(server_mo: Any) -> bool:
    presence = getattr(server_mo, "presence", None)
    if not presence:
        return False
    return str(presence).startswith(_EQUIPPED_PREFIX)


class UcsManagerProvider:
    provider_type = ManagerType.UCS_MANAGER.value

    def __init__(
        self, *, manager: Manager, credentials: ManagerCredentials, timeout_seconds: float
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
        client = self._new_client()
        await client.login()
        try:
            blades = await client.query_classid("computeBlade")
            rack_units = await client.query_classid("computeRackUnit")
            profiles = await client.query_classid("lsServer")
            templates = await client.query_classid("lsServiceProfileTemplate")

            profile_by_dn: dict[str, Any] = {p.dn: p for p in profiles}
            template_dn_by_name: dict[str, str] = {
                t.name: t.dn for t in templates if getattr(t, "name", None)
            }

            for server_mo in (*blades, *rack_units):
                if not _is_equipped(server_mo):
                    continue

                mgmt_ifs = await client.query_children(in_dn=server_mo.dn, class_id="mgmtIf")
                mgmt_if = next(
                    (m for m in mgmt_ifs if getattr(m, "access", None) == "out-of-band"), None
                )
                adapter_ifs = await client.query_children(
                    in_dn=server_mo.dn, class_id="adaptorHostEthIf"
                )

                yield compute_unit_to_provider_server(
                    server_mo,
                    manager_id=self._manager.id,
                    site_id=self._manager.site_id,
                    profile_by_dn=profile_by_dn,
                    template_dn_by_name=template_dn_by_name,
                    mgmt_if=mgmt_if,
                    adapter_ifs=adapter_ifs,
                )
        finally:
            await client.logout()
