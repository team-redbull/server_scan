"""`app.infrastructure.providers.intersight.provider`.

The provider's whole job is the fleet-wide join: read each sub-resource
once for the entire estate and attach it to the right server by following
the inverse reference every child object carries. Two things about that
carry the risk, and both are pinned here rather than left to a live run.

**A join that silently attaches nothing** looks exactly like a fleet with
no drives and no NICs — a run that reports success while destroying
detail. **A sub-resource query that fails** must degrade to `None` (which
`IngestService` preserves) and be reported, never to `[]` (which
overwrites).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from app.domain.enums import ManagerType
from app.domain.models.common import AuditFields
from app.domain.models.manager import Manager
from app.domain.ports.credentials import ManagerConnection
from app.infrastructure.providers.intersight.client import IntersightError
from app.infrastructure.providers.intersight.provider import IntersightProvider

pytestmark = pytest.mark.unit


def _ref(moid: str) -> dict[str, str]:
    """
    An `mo.MoRef` relationship value.

    Args:
        moid (str): The referenced object.

    Returns:
        dict[str, str]: The reference.
    """
    return {"ClassId": "mo.MoRef", "Moid": moid}


# One rack server with one adapter carrying one uplink and one vNIC, one
# storage controller with one disk, one GPU and one BMC interface — the
# smallest estate that exercises every join, including the two-hop ones
# (interfaces reach the server through their adapter unit, disks through
# their controller).
_TABLES: dict[str, list[dict[str, Any]]] = {
    "compute/PhysicalSummaries": [
        {
            "Moid": "server1",
            "Name": "UCSC-C240-WZP1",
            "Model": "UCSC-C240-M6SX",
            "Serial": "WZP1",
            "TotalMemory": 1024,
            "ManagementMode": "Intersight",
            "MgmtIpAddress": "10.0.0.1",
        },
        {
            "Moid": "server2",
            "Name": "UCSC-C220-WZP2",
            "Serial": "WZP2",
            "ManagementMode": "IntersightStandalone",
        },
    ],
    "server/Profiles": [
        {
            "Moid": "profile1",
            "Name": "ocp4-prod-tlv-infra-01",
            "Dn": "profile/1",
            "AssignedServer": _ref("server1"),
            "SrcTemplate": _ref("template1"),
        }
    ],
    "server/ProfileTemplates": [{"Moid": "template1", "Name": "ocp-infra-template"}],
    "adapter/Units": [{"Moid": "adapter1", "ComputeRackUnit": _ref("server1")}],
    "adapter/ExtEthInterfaces": [
        {
            "Moid": "ext1",
            "AdapterUnit": _ref("adapter1"),
            "SwitchId": "A",
            "ExtEthInterfaceId": "1",
            "MacAddress": "00:11:22:33:44:00",
            "OperState": "up",
        }
    ],
    "adapter/HostEthInterfaces": [
        {
            "Moid": "host1",
            "AdapterUnit": _ref("adapter1"),
            "Name": "eth0",
            "MacAddress": "00:AA:BB:CC:DD:EE",
        }
    ],
    "storage/Controllers": [{"Moid": "ctrl1", "ComputeRackUnit": _ref("server1")}],
    "storage/PhysicalDisks": [
        {
            "Moid": "disk1",
            "DiskId": "1",
            "Model": "MZ7LH960",
            "NonCoercedSizeBytes": 960197124096,
            "Health": "Good",
            "StorageController": _ref("ctrl1"),
        }
    ],
    "graphics/Cards": [
        {"Moid": "gpu1", "Model": "NVIDIA A100", "ComputeRackUnit": _ref("server1")}
    ],
    "management/Controllers": [{"Moid": "bmc1", "ComputeRackUnit": _ref("server1")}],
    "management/Interfaces": [
        {"Moid": "mgmt1", "MacAddress": "00:BB:CC:DD:EE:FF", "ManagementController": _ref("bmc1")}
    ],
}


class _FakeClient:
    """
    A scripted stand-in for `IntersightClient`.

    Records every resource asked for, so the request plan itself — one
    list call per resource, never one per server — is assertable.
    """

    def __init__(
        self,
        tables: dict[str, list[dict[str, Any]]] | None = None,
        *,
        failing: frozenset[str] = frozenset(),
    ) -> None:
        """
        Args:
            tables (dict[str, list[dict[str, Any]]] | None): Rows per
                resource, defaulting to the estate above.
            failing (frozenset[str]): Resources whose query raises.
        """
        self.tables = _TABLES if tables is None else tables
        self.failing = failing
        self.requested: list[str] = []
        self.closed = False

    async def list_all(
        self, resource: str, *, select: str | None = None, filter_expr: str | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        """
        Yield the scripted rows for one resource.

        Args:
            resource (str): The resource path.
            select (str | None): Ignored.
            filter_expr (str | None): Applied only for management modes.

        Yields:
            dict[str, Any]: Each row.

        Raises:
            IntersightError: If this resource is scripted to fail.
        """
        self.requested.append(resource)
        if resource in self.failing:
            raise IntersightError(f"{resource} exploded")
        for row in self.tables.get(resource, []):
            if filter_expr and f"'{row.get('ManagementMode')}'" not in filter_expr:
                continue
            yield row

    async def health_check(self) -> None:
        """Always healthy."""
        return

    async def aclose(self) -> None:
        """Record that the client was released."""
        self.closed = True


def _provider(client: _FakeClient, **kwargs: Any) -> IntersightProvider:
    """
    A provider wired to a fake client.

    Args:
        client (_FakeClient): The scripted client.
        **kwargs: Constructor overrides.

    Returns:
        IntersightProvider: The provider under test.
    """
    return IntersightProvider(
        manager=Manager(
            id="mgr_intersight",
            name="intersight",
            type=ManagerType.INTERSIGHT,
            endpoint="intersight.com",
            enabled=True,
            audit=AuditFields.new(),
        ),
        credentials=ManagerConnection(endpoint="intersight.com", username="a/b/c", password="pem"),
        client_factory=lambda: client,
        **kwargs,
    )


async def _collect(provider: IntersightProvider) -> list[Any]:
    """
    Drain a provider.

    Args:
        provider (IntersightProvider): The provider.

    Returns:
        list[Any]: Every server it yielded.
    """
    return [server async for server in provider.list_servers()]


# --- the fleet-wide join ----------------------------------------------


@pytest.mark.asyncio
async def test_every_subresource_is_read_once_for_the_whole_fleet() -> None:
    """The property that makes this collector reach 10,000 servers: cost
    is a function of resource count, not of fleet size. A regression to
    per-server queries would show up here as a repeated resource.
    """
    client = _FakeClient()
    await _collect(_provider(client))

    assert len(client.requested) == len(set(client.requested)), (
        f"a resource was queried more than once: {client.requested}"
    )
    assert "compute/PhysicalSummaries" in client.requested


@pytest.mark.asyncio
async def test_a_two_hop_join_attaches_interfaces_through_their_adapter() -> None:
    """An interface references its adapter unit, and the adapter unit
    references the server — the server is never named on the interface.
    """
    servers = await _collect(_provider(_FakeClient()))
    first = next(s for s in servers if s.serial == "WZP1")

    kinds = sorted(a.interface_kind for a in first.attachments)
    assert kinds == ["PHYSICAL", "VNIC"]
    assert first.nic_macs == ("00:aa:bb:cc:dd:ee",)


@pytest.mark.asyncio
async def test_a_two_hop_join_attaches_disks_through_their_controller() -> None:
    """A `storage.PhysicalDisk` carries no reference to its server."""
    servers = await _collect(_provider(_FakeClient()))
    first = next(s for s in servers if s.serial == "WZP1")

    assert first.storage_drives is not None
    assert [d["model"] for d in first.storage_drives] == ["MZ7LH960"]
    assert first.storage_total_bytes == 960197124096


@pytest.mark.asyncio
async def test_the_profile_supplies_the_name_and_the_template() -> None:
    """The name trap, end to end through the join rather than in
    isolation — this is where a wrong `AssignedServer` key would show.
    """
    servers = await _collect(_provider(_FakeClient()))
    first = next(s for s in servers if s.serial == "WZP1")

    assert first.name == "ocp4-prod-tlv-infra-01"
    assert first.profile_template_name == "ocp-infra-template"


@pytest.mark.asyncio
async def test_a_server_with_no_subresources_gets_empty_not_another_server_s() -> None:
    """The join must not leak one server's hardware onto another — the
    failure mode a naive "first row wins" join produces.
    """
    servers = await _collect(_provider(_FakeClient()))
    second = next(s for s in servers if s.serial == "WZP2")

    assert second.storage_drives == ()
    assert second.attachments == ()
    assert second.gpus == ()
    assert second.name == "UCSC-C220-WZP2"


@pytest.mark.asyncio
async def test_the_bmc_interface_joins_through_its_controller() -> None:
    """Two hops again, and the MAC is the half the summary cannot give."""
    servers = await _collect(_provider(_FakeClient()))
    first = next(s for s in servers if s.serial == "WZP1")

    assert first.bmc_mac == "00:BB:CC:DD:EE:FF"
    assert first.bmc_address_raw == "ipmi://10.0.0.1:623"


# --- overlap with the UCS Central collector ---------------------------


@pytest.mark.asyncio
async def test_ucsm_managed_servers_are_excluded_by_default() -> None:
    """They are exactly the set `..ucs_central` already owns. Collecting
    both makes one document's `source_provider` and fields flip on
    whichever CronJob ran last. ADR-0017, "Decision 3".
    """
    tables = dict(_TABLES)
    tables["compute/PhysicalSummaries"] = [
        {"Moid": "s1", "Name": "imm", "Serial": "A", "ManagementMode": "Intersight"},
        {"Moid": "s2", "Name": "ucsm", "Serial": "B", "ManagementMode": "UCSM"},
    ]
    servers = await _collect(_provider(_FakeClient(tables)))

    assert [s.serial for s in servers] == ["A"]


@pytest.mark.asyncio
async def test_an_operator_can_opt_into_ucsm_mode() -> None:
    """For an estate whose UCS domains are not registered with Central at
    all, where nothing else would ever collect them.
    """
    tables = dict(_TABLES)
    tables["compute/PhysicalSummaries"] = [
        {"Moid": "s2", "Name": "ucsm", "Serial": "B", "ManagementMode": "UCSM"}
    ]
    servers = await _collect(_provider(_FakeClient(tables), management_modes=("UCSM",)))

    assert [s.serial for s in servers] == ["B"]


# --- partial failure --------------------------------------------------


@pytest.mark.asyncio
async def test_a_failed_subresource_query_reports_none_not_empty() -> None:
    """`IngestService` carries a stored value forward for `None` and
    overwrites for a real value, so degrading to `[]` here would clear
    every server's drives — and take a CRITICAL server to HEALTHY by
    reporting that it has no disks to have failed.
    """
    client = _FakeClient(failing=frozenset({"storage/PhysicalDisks"}))
    provider = _provider(client)
    servers = await _collect(provider)

    first = next(s for s in servers if s.serial == "WZP1")
    assert first.storage_drives is None
    assert first.storage_total_bytes is None
    # The rest of the run is unaffected.
    assert first.attachments != ()


@pytest.mark.asyncio
async def test_a_failed_subresource_is_reported_as_a_partial_run() -> None:
    """`tools.run_collector` turns this into exit 3. A run that could not
    read drives is not a run that found none, and must not exit 0.
    """
    provider = _provider(_FakeClient(failing=frozenset({"graphics/Cards"})))
    await _collect(provider)

    assert any("graphics/Cards" in message for message in provider.collection_errors)


@pytest.mark.asyncio
async def test_a_failed_parent_query_skips_its_children_without_crashing() -> None:
    """No adapter units means no way to attribute any interface, so the
    interfaces are unreadable too rather than mis-attributed.
    """
    provider = _provider(_FakeClient(failing=frozenset({"adapter/Units"})))
    servers = await _collect(provider)

    first = next(s for s in servers if s.serial == "WZP1")
    assert first.attachments == ()
    assert first.nic_macs is None


@pytest.mark.asyncio
async def test_a_failing_server_list_aborts_the_run() -> None:
    """Unlike a sub-resource, the anchor query has no partial answer."""
    provider = _provider(_FakeClient(failing=frozenset({"compute/PhysicalSummaries"})))
    with pytest.raises(IntersightError):
        await _collect(provider)


# --- budgets and lifecycle --------------------------------------------


@pytest.mark.asyncio
async def test_an_exhausted_run_budget_stops_and_says_what_it_missed() -> None:
    """Ending with a summary beats being killed by `activeDeadlineSeconds`
    with nothing reported at all.
    """
    provider = _provider(_FakeClient(), run_budget_seconds=-1.0)
    servers = await _collect(provider)

    assert servers == []
    assert any("run budget" in message for message in provider.collection_errors)


@pytest.mark.asyncio
async def test_the_client_is_released_even_when_the_run_fails() -> None:
    """A CronJob pod that leaks its connection pool on every failure is a
    slow leak nobody attributes to the collector.
    """
    client = _FakeClient(failing=frozenset({"compute/PhysicalSummaries"}))
    with pytest.raises(IntersightError):
        await _collect(_provider(client))

    assert client.closed


@pytest.mark.asyncio
async def test_a_clean_run_reports_no_collection_errors() -> None:
    """The negative case, so the partial-run signal means something."""
    provider = _provider(_FakeClient())
    await _collect(provider)

    assert provider.collection_errors == ()


@pytest.mark.asyncio
async def test_the_provider_type_is_stamped_on_every_server() -> None:
    """`Server.source_provider` is what tells an operator which collector
    found a machine, and is filterable in the API.
    """
    provider = _provider(_FakeClient())
    assert provider.provider_type == ManagerType.INTERSIGHT.value

    servers = await _collect(provider)
    assert all(s.manager_id == "mgr_intersight" for s in servers)
