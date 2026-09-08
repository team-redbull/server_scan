"""`..redfish.mapping._dell_serial` — Dell's Service Tag lives in the
`DellSystem` OEM extension, not the generic `ComputerSystem.SerialNumber`
this platform read until 2026-09-08. See docs/dell-collectors.md.
"""

from __future__ import annotations

from app.infrastructure.providers.redfish.mapping import _dell_serial


class TestDellSerial:
    def test_reads_the_node_id_from_the_dell_oem_extension(self) -> None:
        system = {"Oem": {"Dell": {"DellSystem": {"NodeID": "ABC1234"}}}}
        assert _dell_serial(system) == "ABC1234"

    def test_a_non_dell_system_has_no_oem_block(self) -> None:
        assert _dell_serial({"Manufacturer": "HPE"}) is None

    def test_missing_oem_is_none(self) -> None:
        assert _dell_serial({}) is None

    def test_a_malformed_oem_shape_is_none_not_a_crash(self) -> None:
        assert _dell_serial({"Oem": "not a dict"}) is None
        assert _dell_serial({"Oem": {"Dell": "not a dict"}}) is None
        assert _dell_serial({"Oem": {"Dell": {"DellSystem": "not a dict"}}}) is None

    def test_a_placeholder_node_id_is_treated_as_absent(self) -> None:
        system = {"Oem": {"Dell": {"DellSystem": {"NodeID": "Not Specified"}}}}
        assert _dell_serial(system) is None
