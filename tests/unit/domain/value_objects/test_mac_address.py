import pytest

from app.domain.value_objects.mac_address import normalize_mac

CANONICAL = "aa:bb:cc:dd:ee:ff"


@pytest.mark.parametrize(
    "raw",
    [
        "aa:bb:cc:dd:ee:ff",
        "AA:BB:CC:DD:EE:FF",
        "AA-BB-CC-DD-EE-FF",
        "aa-bb-cc-dd-ee-ff",
        "aabb.ccdd.eeff",  # Cisco dotted form
        "AABB.CCDD.EEFF",
        "aabbccddeeff",  # bare hex, as used by map-pxe boot script filenames
        "AABBCCDDEEFF",
        "aa bb cc dd ee ff",
    ],
)
def test_normalizes_all_known_formats_to_canonical_form(raw: str) -> None:
    assert normalize_mac(raw) == CANONICAL


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "00:00:00:00:00:00",  # all-zero: never a real burned-in address
        "ff:ff:ff:ff:ff:ff",  # broadcast
        "aa:bb:cc:dd:ee",  # too short
        "aa:bb:cc:dd:ee:ff:00",  # too long
        "zz:bb:cc:dd:ee:ff",  # non-hex
        "not-a-mac-address",
        "20010db8000000000000000000000001",  # 32-hex IB GUID: out of scope
    ],
)
def test_rejects_invalid_or_out_of_scope_input(raw: str | None) -> None:
    assert normalize_mac(raw) is None
