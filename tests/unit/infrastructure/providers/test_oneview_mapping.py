"""`app.infrastructure.providers.oneview.mapping`.

Pure functions, no I/O. Every trap docs/hpe-collectors.md records gets a
test here, because each one fails *silently* in production: a wrong name
defeats site parsing and classification, a per-socket core count halves
every two-socket server, and a subresource mapped to zero instead of
`None` overwrites good data with an empty fleet.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.domain.enums import Vendor
from app.infrastructure.providers.oneview.mapping import (
    ilo_generation,
    management_processor_address,
    profile_from,
    psus_from,
    server_from,
    subresource_data,
)

pytestmark = pytest.mark.unit


def _profile(**overrides: Any) -> Any:
    """
    A parsed server profile.

    Args:
        **overrides: Fields to override on the raw profile document.

    Returns:
        OneViewProfile: The parsed profile.
    """
    raw = {
        "uri": "/rest/server-profiles/abc",
        "name": "ocp4-tlv-prod-worker-01",
        "serverProfileTemplateUri": "/rest/server-profile-templates/tpl",
    }
    raw.update(overrides)
    parsed = profile_from(raw, template_names={"/rest/server-profile-templates/tpl": "worker-tpl"})
    assert parsed is not None
    return parsed


def _hardware(**overrides: Any) -> dict[str, Any]:
    """
    A Gen10 rack server as `GET /rest/server-hardware?expand=all` reports it.

    Args:
        **overrides: Fields to override.

    Returns:
        dict[str, Any]: The server-hardware member.
    """
    member: dict[str, Any] = {
        "uri": "/rest/server-hardware/30373737-3237-4D32",
        # The trap: a location string for a blade, `ILO<serial>` for a
        # rack server. Never the server's name.
        "name": "ILOUSE31835LS",
        "serverName": "worker01.corp.example.net",
        "serverProfileUri": "/rest/server-profiles/abc",
        "serialNumber": "USE31835LS",
        "uuid": "30373737-3237-4D32-3230-333030524D38",
        "model": "ProLiant DL380 Gen10",
        "shortModel": "DL380 Gen10",
        "mpModel": "iLO5",
        "mpFirmwareVersion": "2.78 Mar 15 2023",
        "memoryMb": 393216,
        "processorCount": 2,
        "processorCoreCount": 24,
        "processorType": "Intel(R) Xeon(R) Gold 6248R CPU @ 3.00GHz",
        "mpHostInfo": {
            "mpHostName": "ilo-worker01.corp.example.net",
            "mpIpAddresses": [
                {"address": "fe80::1", "type": "LinkLocal"},
                {"address": "10.20.30.40", "type": "Static"},
            ],
        },
        "portMap": {
            "deviceSlots": [
                {
                    "deviceName": "HPE Eth 10Gb 2p 562FLR-T Adptr",
                    "slotNumber": "0",
                    "location": "Flr",
                    "physicalPorts": [
                        {
                            "portNumber": 1,
                            "type": "Ethernet",
                            "mac": "AA:BB:CC:DD:EE:01",
                            "virtualPorts": [{"mac": "AA:BB:CC:DD:EE:F1", "portFunction": "a"}],
                        },
                        {"portNumber": 2, "type": "Ethernet", "mac": "AA:BB:CC:DD:EE:02"},
                    ],
                }
            ]
        },
        "subResources": {
            "Devices": {
                "name": "Devices",
                "collectionState": "Collected",
                "data": [
                    {
                        "Id": "1",
                        "DeviceType": "GPU",
                        "Name": "HPE NVIDIA A100 40GB PCIe Accelerator",
                        "Manufacturer": "NVIDIA",
                        "SerialNumber": "GPU-0001",
                        "Location": "PCI-E Slot 1",
                        "Status": {"Health": "OK", "State": "Enabled"},
                    },
                    {
                        "Id": "2",
                        "DeviceType": "LOM/NIC",
                        "Name": "HPE Eth 10Gb 2p 521T Adptr",
                        "Status": {"Health": "OK", "State": "Enabled"},
                    },
                    {
                        "Id": "3",
                        "DeviceType": "Unknown",
                        "Name": "Empty slot 2",
                        "Status": {"Health": None, "State": "Absent"},
                    },
                ],
            },
            "LocalStorageV2": {
                "name": "LocalStorageV2",
                "collectionState": "Collected",
                "data": {
                    "Drives": [
                        {
                            "Id": "0",
                            "Model": "MO001600JWFWU",
                            "SerialNumber": "S4H0NA0M",
                            "MediaType": "SSD",
                            "Protocol": "NVMe",
                            "CapacityBytes": 1600321314816,
                            "Status": {"Health": "OK", "State": "Enabled"},
                        }
                    ]
                },
            },
        },
    }
    member.update(overrides)
    return member


def _mapped(**overrides: Any) -> Any:
    """
    Map the sample server.

    Args:
        **overrides: Fields to override on the hardware member.

    Returns:
        ProviderServer: The mapped server.
    """
    return server_from(
        hardware=_hardware(**overrides), profile=_profile(), manager_id="mgr_oneview"
    )


# --- trap 1: the name -------------------------------------------------


class TestName:
    def test_the_name_comes_from_the_profile_not_the_hardware(self) -> None:
        """`server-hardware.name` is `"Encl1, bay 3"` for a blade and
        `ILO<serial>` for a rack server; `serverName` is an OS hostname
        reported through HPE AMS. Only the profile carries the name site
        parsing and classification key off — the exact trap that named
        every UCS server after its chassis slot.
        """
        server = _mapped()

        assert server.name == "ocp4-tlv-prod-worker-01"
        assert server.name != "ILOUSE31835LS"
        assert server.name != "worker01.corp.example.net"

    def test_the_profile_uri_is_carried_as_the_profile_identity(self) -> None:
        server = _mapped()

        assert server.profile_dn == "/rest/server-profiles/abc"
        assert server.profile_template_name == "worker-tpl"
        assert server.profile_template_external_id == "/rest/server-profile-templates/tpl"

    def test_a_profile_without_a_uri_or_a_name_is_unusable(self) -> None:
        assert profile_from({"name": "ocp4-x"}) is None
        assert profile_from({"uri": "/rest/server-profiles/x"}) is None

    def test_the_external_id_is_the_hardware_uri(self) -> None:
        assert _mapped().external_id == "/rest/server-hardware/30373737-3237-4D32"

    def test_the_serial_is_the_hardware_one_not_the_profile_one(self) -> None:
        """A profile's own `serialNumber` defaults to a *virtual* serial,
        and ingest correlates on `(vendor, serial_normalized)` — a
        virtual serial would split one machine into two documents.
        """
        server = _mapped()

        assert server.serial == "USE31835LS"
        assert server.vendor == Vendor.HP.value


# --- trap 2: cores per processor --------------------------------------


class TestCpu:
    def test_cores_are_multiplied_by_the_socket_count(self) -> None:
        """`processorCoreCount` is documented as "Number of cores
        available **per processor**", while this platform's `cpu_cores`
        is whole-system. Reporting it unmultiplied halves every
        two-socket server, silently and forever.
        """
        server = _mapped()

        assert server.cpu_sockets == 2
        assert server.cpu_cores == 48

    @pytest.mark.parametrize(
        "overrides",
        [
            {"processorCount": None},
            {"processorCoreCount": None},
            {"processorCount": 0},
            {"processorCoreCount": 0},
        ],
    )
    def test_a_missing_half_of_the_product_reports_unread(self, overrides: dict[str, Any]) -> None:
        """Half of a multiplication is not a core count. `None` lets
        ingest carry the stored value forward; a zero would overwrite it.
        """
        assert _mapped(**overrides).cpu_cores is None

    def test_threads_are_never_guessed(self) -> None:
        """OneView reports no thread count anywhere on `server-hardware`,
        and `2 x cores` is the heuristic ADR-0020 deleted.
        """
        assert _mapped().cpu_threads is None

    def test_the_cpu_model_string_is_passed_through(self) -> None:
        assert _mapped().cpu_model == "Intel(R) Xeon(R) Gold 6248R CPU @ 3.00GHz"


# --- trap 3: memoryMb is MiB ------------------------------------------


class TestMemory:
    def test_memory_is_converted_from_mib(self) -> None:
        """HPE documents the unit inline and spells out the factor:
        "in MiB (1 MiB = 1,048,576 bytes)". No assumption needed, unlike
        Intersight's undocumented `TotalMemory`.
        """
        assert _mapped().memory_total_bytes == 393216 * 1024 * 1024
        assert _mapped().memory_total_bytes == 384 * 1024**3

    def test_absent_memory_is_unread_not_zero(self) -> None:
        assert _mapped(memoryMb=None).memory_total_bytes is None
        assert _mapped(memoryMb=0).memory_total_bytes is None


# --- trap 7: InsufficientFirmware -------------------------------------


class TestSubresourceStates:
    @pytest.mark.parametrize(
        "state",
        ["InsufficientFirmware", "CollectionError", "CollectedStale", "NotCollected", "Unknown"],
    )
    def test_any_state_but_collected_reports_unread(self, state: str) -> None:
        """An iLO-4 server answers `InsufficientFirmware` for every
        subresource ("The minimum version to collect some types of
        inventory is iLO 5 v1.20"). Mapping that to an empty list would
        report zero drives — which once took a machine from CRITICAL to
        HEALTHY and logged that the drive had recovered.

        `CollectedStale` is excluded too: HPE defines it as data that may
        be "out of date **or missing** due to the server state".
        """
        member = _hardware()
        member["subResources"]["Devices"]["collectionState"] = state
        member["subResources"]["LocalStorageV2"]["collectionState"] = state
        server = server_from(hardware=member, profile=_profile(), manager_id="mgr_oneview")

        assert server.gpus is None
        assert server.storage_drives is None
        assert server.storage_total_bytes is None

    def test_an_ilo4_server_still_reports_its_identity(self) -> None:
        """The whole point of the OneView-only design: an iLO-4 server
        Redfish would reject is still named, serialled and addressed.
        """
        member = _hardware(mpModel="iLO4", mpFirmwareVersion="2.44")
        member["subResources"]["Devices"]["collectionState"] = "InsufficientFirmware"
        member["subResources"]["LocalStorageV2"]["collectionState"] = "InsufficientFirmware"
        server = server_from(hardware=member, profile=_profile(), manager_id="mgr_oneview")

        assert server.name == "ocp4-tlv-prod-worker-01"
        assert server.serial == "USE31835LS"
        assert server.bmc_address_raw == "https://10.20.30.40"

    def test_a_collected_but_empty_subresource_is_empty_not_unread(self) -> None:
        """The other half of the contract: "read, and there are none" is
        a real answer and must not be reported as `None`.
        """
        member = _hardware()
        member["subResources"]["Devices"]["data"] = []
        server = server_from(hardware=member, profile=_profile(), manager_id="mgr_oneview")

        assert server.gpus == ()

    def test_an_absent_subresources_container_reports_unread(self) -> None:
        assert subresource_data({}, "Devices") is None

    def test_subresources_as_an_array_is_read_the_same_as_an_object(self) -> None:
        """HPE documents the per-subresource fields but not the shape of
        their container, so both are accepted rather than guessed.
        """
        member = _hardware()
        member["subResources"] = list(member["subResources"].values())
        rows = subresource_data(member, "Devices")

        assert rows is not None
        assert len(rows) == 3


# --- trap 8: GPUs -----------------------------------------------------


class TestGpus:
    def test_only_gpu_devices_that_are_present_are_reported(self) -> None:
        """The `Devices` array carries NICs and empty slots too; HPE's own
        example shows an empty bay as `Status.State: "Absent"`.
        """
        gpus = _mapped().gpus

        assert gpus is not None
        assert len(gpus) == 1
        assert gpus[0]["model"] == "HPE NVIDIA A100 40GB PCIe Accelerator"
        assert gpus[0]["vendor"] == "NVIDIA"
        assert gpus[0]["serial"] == "GPU-0001"
        assert gpus[0]["pci_address"] == "PCI-E Slot 1"

    def test_gpu_memory_is_left_for_the_catalog_to_fill(self) -> None:
        """OneView reports no GPU memory field anywhere — not on the
        device, not on the server, not in the `Processor` schema. Guessing
        one would be worse than the catalog's honest miss.
        """
        gpus = _mapped().gpus

        assert gpus is not None
        assert gpus[0]["memory_bytes"] is None

    def test_the_reported_model_string_matches_the_gpu_catalog(self) -> None:
        """The end-to-end claim: OneView's HPE-branded product name is
        what `GpuCatalog` has to recognise, since it is the only route to
        a VRAM figure for an HPE card.
        """
        from app.domain.value_objects.gpu_catalog import GpuCatalog

        gpus = _mapped().gpus
        assert gpus is not None
        enriched = GpuCatalog.from_spec("").enrich(gpus[0])

        assert enriched["model"] == "NVIDIA A100 40GB"
        assert enriched["memory_bytes"] == 40 * 1024**3


# --- storage ----------------------------------------------------------


class TestStorage:
    def test_localstoragev2_capacity_is_taken_in_bytes(self) -> None:
        server = _mapped()

        assert server.storage_drives is not None
        assert server.storage_drives[0]["capacity_bytes"] == 1600321314816
        assert server.storage_drives[0]["media_type"] == "NVME"
        assert server.storage_total_bytes == 1600321314816

    def test_the_v1_schema_never_uses_the_marketing_capacity(self) -> None:
        """`CapacityGB` is documented by HPE as "the marketing capacity
        (base 10)". `CapacityMiB` is the real figure, and `SMR` is a hard
        disk the Redfish enum has no member for.
        """
        member = _hardware()
        member["subResources"] = {
            "LocalStorage": {
                "name": "LocalStorage",
                "collectionState": "Collected",
                "data": {
                    "PhysicalDrives": [
                        {
                            "Id": "1I:1:1",
                            "Model": "MB016000JWZFF",
                            "SerialNumber": "ZL2A",
                            "MediaType": "SMR",
                            "InterfaceType": "SATA",
                            "Location": "Port 1I Box 1 Bay 1",
                            "CapacityGB": 16000,
                            "CapacityMiB": 15259721,
                            "Status": {"Health": "OK", "State": "Enabled"},
                        }
                    ]
                },
            }
        }
        server = server_from(hardware=member, profile=_profile(), manager_id="mgr_oneview")

        assert server.storage_drives is not None
        assert server.storage_drives[0]["capacity_bytes"] == 15259721 * 1024 * 1024
        assert server.storage_drives[0]["media_type"] == "HDD"

    def test_the_v1_schema_falls_back_to_blocks_times_block_size(self) -> None:
        member = _hardware()
        member["subResources"] = {
            "LocalStorage": {
                "name": "LocalStorage",
                "collectionState": "Collected",
                "data": {
                    "PhysicalDrives": [
                        {
                            "Id": "1I:1:2",
                            "MediaType": "HDD",
                            "CapacityGB": 900,
                            "CapacityLogicalBlocks": 1758174768,
                            "BlockSizeBytes": 512,
                        }
                    ]
                },
            }
        }
        server = server_from(hardware=member, profile=_profile(), manager_id="mgr_oneview")

        assert server.storage_drives is not None
        assert server.storage_drives[0]["capacity_bytes"] == 1758174768 * 512

    def test_v2_wins_where_a_server_reports_both(self) -> None:
        """A Gen10-Plus adapter provides V2 "instead of (or in addition
        to)" V1, and V2 is stock Redfish with no marketing-capacity field
        to pick wrongly.
        """
        member = _hardware()
        member["subResources"]["LocalStorage"] = {
            "name": "LocalStorage",
            "collectionState": "Collected",
            "data": {"PhysicalDrives": [{"Id": "old", "CapacityMiB": 1}]},
        }
        server = server_from(hardware=member, profile=_profile(), manager_id="mgr_oneview")

        assert server.storage_drives is not None
        assert server.storage_drives[0]["id"] == "0"


# --- NICs and the management-processor address ------------------------


class TestNetwork:
    def test_only_physical_port_macs_reach_the_correlation_set(self) -> None:
        """`virtualPorts` are FlexNICs carved out of a physical port.
        Feeding both levels in flat would inflate `nic_macs`, which is an
        identity-correlation input.
        """
        server = _mapped()

        # Normalized at the provider boundary, per `ProviderServer`.
        assert server.nic_macs == ("aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:02")
        assert len(server.nics) == 2

    def test_link_speed_and_state_are_never_synthesised(self) -> None:
        """Neither exists anywhere in `portMap`."""
        server = _mapped()

        assert all(nic.speed_mbps is None for nic in server.nics)
        assert all(nic.link_state == "UNKNOWN" for nic in server.nics)

    def test_an_absent_port_map_reports_unread_not_empty(self) -> None:
        server = _mapped(portMap=None)

        assert server.nic_macs is None
        assert server.nics == ()

    def test_link_local_addresses_are_skipped_and_static_preferred(self) -> None:
        """An IPv6 link-local address is unroutable without a zone index,
        which nothing downstream carries.
        """
        assert _mapped().bmc_address_raw == "https://10.20.30.40"

    def test_dhcp_is_used_when_no_static_address_exists(self) -> None:
        member = _hardware()
        member["mpHostInfo"]["mpIpAddresses"] = [
            {"address": "fe80::1", "type": "LinkLocal"},
            {"address": "10.1.1.1", "type": "DHCP"},
        ]

        assert management_processor_address(member) == "10.1.1.1"

    def test_the_host_name_is_the_last_resort(self) -> None:
        member = _hardware()
        member["mpHostInfo"]["mpIpAddresses"] = [{"address": "fe80::1", "type": "LinkLocal"}]

        assert management_processor_address(member) == "ilo-worker01.corp.example.net"

    def test_no_address_at_all_is_reported_as_none(self) -> None:
        assert management_processor_address({"mpHostInfo": {}}) is None
        assert management_processor_address({}) is None


# --- iLO generation ---------------------------------------------------


class TestIloGeneration:
    @pytest.mark.parametrize(
        ("mp_model", "expected"),
        [("iLO4", 4), ("iLO5", 5), ("iLO 6", 6), ("iLO7", 7), ("iLO5 ", 5)],
    )
    def test_the_trailing_integer_is_parsed(self, mp_model: str, expected: int) -> None:
        """`mpModel` is documented with exactly one example value and no
        enum, so equality against a guessed string would route every
        server down the wrong branch.
        """
        assert ilo_generation(mp_model) == expected

    @pytest.mark.parametrize("mp_model", [None, "", "iLO", "Unknown"])
    def test_an_unparseable_value_is_unknown_not_old(self, mp_model: object) -> None:
        assert ilo_generation(mp_model) is None


class TestPsus:
    """PSUs are the one thing OneView will not hand over in the bulk
    sweep, and the one field no provider in this repo has ever populated
    — the health engine's `power.psu_count` and `power.failed_psu_count`
    have had nothing to read since they were written.
    """

    def test_no_power_supply_data_is_unread_not_empty(self) -> None:
        assert _mapped().psus is None

    def test_the_hpe_state_decides_health_not_a_boolean(self) -> None:
        """OneView separates a PSU that lost AC input from one that is
        degraded from one that failed outright. Flattening those to
        healthy/unhealthy throws away the distinction the health engine
        can act on.
        """
        rows = [
            {
                "MemberId": "0",
                "Model": "865414-B21",
                "SerialNumber": "5WBXK0GLLDF123",
                "PowerCapacityWatts": 800,
                "Status": {"Health": "OK", "State": "Enabled"},
                "Oem": {"Hpe": {"PowerSupplyStatus": {"State": "Ok"}}},
            },
            {
                "MemberId": "1",
                "PowerCapacityWatts": 800,
                # Redfish says OK; HPE says the wall socket is dead.
                "Status": {"Health": "OK", "State": "Enabled"},
                "Oem": {"Hpe": {"PowerSupplyStatus": {"State": "ACPowerLost"}}},
            },
            {
                "MemberId": "2",
                "Status": {"Health": "OK", "State": "Enabled"},
                "Oem": {"Hpe": {"PowerSupplyStatus": {"State": "Degraded"}}},
            },
        ]
        psus = psus_from(rows)

        assert psus is not None
        assert [p["health"] for p in psus] == ["HEALTHY", "CRITICAL", "WARNING"]
        assert psus[0]["capacity_watts"] == 800
        assert psus[0]["serial"] == "5WBXK0GLLDF123"

    def test_an_unmapped_state_falls_back_to_the_redfish_health(self) -> None:
        psus = psus_from(
            [
                {"MemberId": "0", "Status": {"Health": "Critical"}, "Oem": {"Hpe": {}}},
                {"MemberId": "1", "Status": {"Health": "OK"}},
            ]
        )

        assert psus is not None
        assert [p["health"] for p in psus] == ["CRITICAL", "HEALTHY"]

    def test_an_absent_bay_is_not_a_power_supply(self) -> None:
        assert psus_from([{"MemberId": "1", "Status": {"State": "Absent"}}]) == ()

    def test_unread_stays_unread(self) -> None:
        assert psus_from(None) is None

    def test_power_supplies_reach_the_provider_server(self) -> None:
        server = server_from(
            hardware=_hardware(),
            profile=_profile(),
            manager_id="mgr_oneview",
            power_supplies=[
                {"MemberId": "0", "Oem": {"Hpe": {"PowerSupplyStatus": {"State": "Failed"}}}}
            ],
        )

        assert server.psus is not None
        assert server.psus[0]["health"] == "CRITICAL"
