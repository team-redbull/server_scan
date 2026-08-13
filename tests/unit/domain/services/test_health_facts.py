from datetime import UTC, datetime

from app.domain.enums import LinkState
from app.domain.models.connectivity import Connectivity, ConnectivityFacts
from app.domain.models.hardware import Hardware, Power, Psu, Storage, StorageDrive
from app.domain.models.network import NetworkInfo, NetworkInterface
from app.domain.models.server import Server
from app.domain.services.health.facts import extract_facts

NOW = datetime.now(UTC)


def test_extract_facts_on_empty_server_returns_zeroed_facts() -> None:
    server = Server(_id="srv_x", name="x", created_at=NOW, updated_at=NOW)
    facts = extract_facts(server)
    assert facts["storage.drive_count"] == 0
    assert facts["storage.failed_drive_count"] == 0
    assert facts["connectivity.fabric_paths_down"] == 0


def test_extract_facts_counts_failed_drives() -> None:
    server = Server(
        _id="srv_x",
        name="x",
        created_at=NOW,
        updated_at=NOW,
        hardware=Hardware(
            storage=Storage(
                drives=[
                    StorageDrive(id="d1", health="FAILED"),
                    StorageDrive(id="d2", health="OK"),
                    StorageDrive(id="d3", health="FAILED"),
                ]
            )
        ),
    )
    facts = extract_facts(server)
    assert facts["storage.drive_count"] == 3
    assert facts["storage.failed_drive_count"] == 2
    assert facts["storage.drive_healths"] == ["FAILED", "OK", "FAILED"]


def test_extract_facts_reads_connectivity_facts_directly() -> None:
    server = Server(
        _id="srv_x",
        name="x",
        created_at=NOW,
        updated_at=NOW,
        connectivity=Connectivity(
            facts=ConnectivityFacts(fabric_paths_total=2, fabric_paths_up=1, fabric_paths_down=1)
        ),
    )
    facts = extract_facts(server)
    assert facts["connectivity.fabric_paths_up"] == 1
    assert facts["connectivity.fabric_paths_down"] == 1


def test_extract_facts_counts_failed_psus() -> None:
    server = Server(
        _id="srv_x",
        name="x",
        created_at=NOW,
        updated_at=NOW,
        hardware=Hardware(
            power=Power(psus=[Psu(id="p1", health="OK"), Psu(id="p2", health="FAILED")])
        ),
    )
    facts = extract_facts(server)
    assert facts["power.psu_count"] == 2
    assert facts["power.failed_psu_count"] == 1


def test_extract_facts_link_states() -> None:
    server = Server(
        _id="srv_x",
        name="x",
        created_at=NOW,
        updated_at=NOW,
        network=NetworkInfo(
            interfaces=[
                NetworkInterface(name="nic1", link_state=LinkState.UP),
                NetworkInterface(name="nic2", link_state=LinkState.DOWN),
            ]
        ),
    )
    facts = extract_facts(server)
    assert facts["network.interface_link_states"] == ["UP", "DOWN"]
