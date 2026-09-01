"""Regression tests for the three defects ADR-0016 found in shipped code.

Each test fails without its fix. They live together because they share one
root cause — the pipeline could not tell "the collector read this and found
nothing" from "the collector could not read this at all" — which is exactly
the distinction DSP0266 §9.6.1 draws between an absent property and a null
one, and which a fan-out collector over hundreds of independent BMCs hits
routinely rather than exceptionally.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from app.application.services.ingest import IngestService
from app.domain.enums import HealthSeverity, MediaType
from app.domain.models.health import Health
from app.domain.ports.provider import ProviderServer
from app.domain.value_objects.site import site_catalog
from app.infrastructure.mongodb import MongoClientHolder
from app.infrastructure.mongodb.manager_repository import MongoManagerRepository
from app.infrastructure.mongodb.server_repository import MongoServerRepository
from app.infrastructure.mongodb.site_repository import MongoSiteRepository

SITES = site_catalog("")

pytestmark = pytest.mark.integration

_CURSOR_SECRET = "test-cursor-secret"


class _OneShotProvider:
    """Yields exactly the `ProviderServer`s it is handed.

    Lets a test state a provider's output directly, including the `None`s a
    real collector emits for a sub-resource it could not read.
    """

    provider_type = "test"

    def __init__(self, *servers: ProviderServer) -> None:
        self._servers = servers

    async def health_check(self) -> None:
        return

    async def list_servers(self) -> AsyncIterator[ProviderServer]:
        for server in self._servers:
            yield server


def _service(mongo: MongoClientHolder) -> IngestService:
    return IngestService(
        sites=SITES,
        server_repo=MongoServerRepository(mongo, cursor_secret=_CURSOR_SECRET),
        site_repo=MongoSiteRepository(mongo),
        manager_repo=MongoManagerRepository(mongo),
    )


def _fully_read(**overrides: object) -> ProviderServer:
    """A server whose collector read every field successfully."""
    base: dict[str, object] = {
        "external_id": "redfish://10.20.30.41/redfish/v1/Systems/1",
        "vendor": "dell",
        "name": "ocp4-prod-tlv-infra-01",
        "serial": "SN-PARTIAL-1",
        "system_uuid": "11111111-2222-3333-4444-555555555555",
        "nic_macs": ("00:00:5e:00:53:01",),
        "cpu_sockets": 2,
        "cpu_cores": 64,
        "cpu_threads": 128,
        "cpu_model": "Xeon Gold 6338",
        "memory_total_bytes": 512 * 1024**3,
        "storage_total_bytes": 4 * 1024**4,
        "storage_drives": (
            {
                "id": "/redfish/v1/Chassis/1/Drives/0",
                "model": "MZ7LH3T8",
                "serial": "DRIVE-1",
                "media_type": MediaType.SSD.value,
                "capacity_bytes": 4 * 1024**4,
                "health": HealthSeverity.CRITICAL.value,
            },
        ),
        "psus": ({"id": "1", "model": "PSU-750W", "serial": "PSU-1", "health": "DOWN"},),
    }
    base.update(overrides)
    return ProviderServer(**base)  # type: ignore[arg-type]


async def test_a_sub_resource_that_could_not_be_read_does_not_erase_stored_hardware(
    mongo_holder: MongoClientHolder,
) -> None:
    """The defect that motivated ADR-0016's port change.

    A Redfish host whose `Storage` collection 404s — which sushy 5.10.0 had
    to handle, because HGX boards advertise members that 404 — used to
    report zeros that overwrote good data. `storage.failed_drive_count`
    then fell to 0, so the seeded `storage.failed_drive` policy stopped
    firing: a server with a genuinely failed disk healed itself by not
    being read.
    """
    service = _service(mongo_holder)

    await service.ingest(_OneShotProvider(_fully_read()))

    # Same host, next run: Processors/Memory/Storage/EthernetInterfaces all
    # failed. `None`, not zero — the collector is saying "I don't know",
    # not "there are none".
    summary = await service.ingest(
        _OneShotProvider(
            _fully_read(
                cpu_sockets=None,
                cpu_cores=None,
                cpu_threads=None,
                cpu_model=None,
                memory_total_bytes=None,
                storage_total_bytes=None,
                storage_drives=None,
                nic_macs=None,
                psus=None,
            )
        )
    )

    # Without the carry-forward this is 1, not 0: the provider's `None`
    # reaches `Cpu(sockets=...)`, pydantic rejects it, and the per-server
    # handler counts an error. The stored document survives by accident,
    # which is why the assertions below are not sufficient on their own.
    assert summary.errors == 0
    assert summary.updated == 1

    repo = MongoServerRepository(mongo_holder, cursor_secret=_CURSOR_SECRET)
    page = await repo.list_page(
        filters={"identity.serial_normalized": "sn-partial-1"},
        search=None,
        sort="name",
        sort_desc=False,
        cursor=None,
        page_size=1,
        with_count=False,
    )
    server = page.items[0]

    assert server.hardware.storage.total_bytes == 4 * 1024**4
    assert [d.health for d in server.hardware.storage.drives] == [HealthSeverity.CRITICAL.value]
    assert server.hardware.cpu.sockets == 2
    assert server.hardware.cpu.model == "Xeon Gold 6338"
    assert server.hardware.memory.total_bytes == 512 * 1024**3
    assert server.identity.nic_macs == ["00:00:5e:00:53:01"]
    # Added 2026-09-01: `psus` follows the identical carry-forward
    # contract — `IngestService` used to hardcode `Power(psus=[])`
    # unconditionally, which this same-shaped defect would have produced
    # regardless of what the provider reported.
    assert [p.health for p in server.hardware.power.psus] == ["DOWN"]


async def test_an_empty_read_still_overwrites(mongo_holder: MongoClientHolder) -> None:
    """The other half of the contract, and the reason `None` had to be a
    distinct value rather than reusing the zero.

    A collector that successfully read a host and found no drives must be
    able to say so — pulling a disk is a real event the inventory has to
    reflect.
    """
    service = _service(mongo_holder)
    await service.ingest(_OneShotProvider(_fully_read(serial="SN-PARTIAL-2")))
    await service.ingest(
        _OneShotProvider(
            _fully_read(serial="SN-PARTIAL-2", storage_total_bytes=0, storage_drives=())
        )
    )

    repo = MongoServerRepository(mongo_holder, cursor_secret=_CURSOR_SECRET)
    page = await repo.list_page(
        filters={"identity.serial_normalized": "sn-partial-2"},
        search=None,
        sort="name",
        sort_desc=False,
        cursor=None,
        page_size=1,
        with_count=False,
    )
    assert page.items[0].hardware.storage.drives == []
    assert page.items[0].hardware.storage.total_bytes == 0


async def test_a_partial_read_does_not_write_a_health_recovery_event(
    mongo_holder: MongoClientHolder,
) -> None:
    """The defect's second-order harm.

    `_emit_transition_events` compares health before and after, so zeroed
    storage did not merely lose data — it wrote a durable
    HEALTH_STATUS_CHANGED event asserting the failed drive had recovered.
    """
    service = _service(mongo_holder)
    await service.ingest(_OneShotProvider(_fully_read(serial="SN-PARTIAL-3")))

    repo = MongoServerRepository(mongo_holder, cursor_secret=_CURSOR_SECRET)
    page = await repo.list_page(
        filters={"identity.serial_normalized": "sn-partial-3"},
        search=None,
        sort="name",
        sort_desc=False,
        cursor=None,
        page_size=1,
        with_count=False,
    )
    stored = page.items[0]
    stored.health = Health(overall=HealthSeverity.CRITICAL, storage=HealthSeverity.CRITICAL)
    await repo.upsert(stored)

    summary = await service.ingest(
        _OneShotProvider(
            _fully_read(serial="SN-PARTIAL-3", storage_total_bytes=None, storage_drives=None)
        )
    )
    assert summary.errors == 0

    page = await repo.list_page(
        filters={"identity.serial_normalized": "sn-partial-3"},
        search=None,
        sort="name",
        sort_desc=False,
        cursor=None,
        page_size=1,
        with_count=False,
    )
    assert page.items[0].health.overall is HealthSeverity.CRITICAL


async def test_two_servers_without_a_system_uuid_can_both_be_ingested(
    mongo_holder: MongoClientHolder,
) -> None:
    """`uniq_system_uuid`'s partial filter used `$exists: true`, which
    MongoDB satisfies for a field that is present *and null* — and
    `model_dump(mode="json")` always emits the key.

    So every UUID-less server entered a unique index keyed on null, and
    exactly one of them could exist fleet-wide. `ComputerSystem.UUID` is
    schema-optional, so this blocked OpenBMC whiteboxes and older firmware
    outright.
    """
    # `mongo_holder` already ran `ensure_indexes`, so `uniq_system_uuid`
    # is present with its declared specification — re-creating it here
    # would race the fixture's own migration.
    service = _service(mongo_holder)
    summary = await service.ingest(
        _OneShotProvider(
            _fully_read(serial="SN-NOUUID-1", system_uuid=None, name="ocp4-one-a"),
            _fully_read(serial="SN-NOUUID-2", system_uuid=None, name="ocp4-one-b"),
        )
    )

    assert summary.errors == 0
    assert summary.fetched == 2
