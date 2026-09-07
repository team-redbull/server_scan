"""`..redfish.mapping.health_detail_of` and its wiring into `drive_to_dict`/
`gpus_from_processors` — the raw `Status.Health` string `health_of` reduces,
kept alongside it for diagnosis. Never read by the health policy engine; see
`app.domain.models.hardware.StorageDrive.health_detail`'s docstring for the
platform-wide contract this implements.
"""

from __future__ import annotations

from app.infrastructure.providers.redfish.mapping import (
    drive_to_dict,
    gpus_from_processors,
    health_detail_of,
)


class TestHealthDetailOf:
    def test_returns_the_raw_status_health_string(self) -> None:
        assert health_detail_of({"Status": {"Health": "Critical"}}) == "Critical"

    def test_missing_status_is_none(self) -> None:
        assert health_detail_of({}) is None

    def test_non_string_health_is_none(self) -> None:
        assert health_detail_of({"Status": {"Health": None}}) is None

    def test_empty_health_is_none(self) -> None:
        assert health_detail_of({"Status": {"Health": ""}}) is None


class TestDriveHealthDetail:
    def test_a_drives_raw_health_string_is_carried(self) -> None:
        drive = drive_to_dict({"Id": "0", "Status": {"Health": "Warning"}})
        assert drive["health"] == "WARNING"
        assert drive["health_detail"] == "Warning"

    def test_a_drive_with_no_status_carries_no_detail(self) -> None:
        drive = drive_to_dict({"Id": "0"})
        assert drive["health"] == "UNKNOWN"
        assert drive["health_detail"] is None


class TestGpuHealthDetail:
    def test_a_gpus_raw_health_string_is_carried(self) -> None:
        gpus = gpus_from_processors([{"ProcessorType": "GPU", "Status": {"Health": "Critical"}}])
        assert gpus is not None
        [gpu] = gpus
        assert gpu["health"] == "CRITICAL"
        assert gpu["health_detail"] == "Critical"
