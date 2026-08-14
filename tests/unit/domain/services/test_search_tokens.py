from datetime import UTC, datetime

from app.domain.enums import InstallationType, Vendor
from app.domain.models.network import BmcInfo, NetworkInfo
from app.domain.models.server import Identity, Server
from app.domain.services.search_tokens import build_search_tokens

NOW = datetime.now(UTC)


def _server(**overrides: object) -> Server:
    defaults: dict[str, object] = {
        "_id": "srv_test",
        "name": "ocp-dell-worker-001",
        # Required now that `Identity.vendor` has no default; overridable
        # by the tests that actually care about vendor.
        "identity": Identity(vendor=Vendor.DELL),
        "created_at": NOW,
        "updated_at": NOW,
    }
    defaults.update(overrides)
    return Server(**defaults)  # type: ignore[arg-type]


def test_splits_hyphenated_name_into_tokens() -> None:
    tokens = build_search_tokens(_server(name="ocp-dell-worker-001"))
    assert "ocp-dell-worker-001" in tokens  # full value retained
    assert "ocp" in tokens
    assert "dell" in tokens
    assert "worker" in tokens
    assert "001" in tokens


def test_includes_both_mac_forms() -> None:
    server = _server(
        network=NetworkInfo(bmc=BmcInfo(mac="aa:bb:cc:dd:ee:ff")),
    )
    tokens = build_search_tokens(server)
    assert "aa:bb:cc:dd:ee:ff" in tokens
    assert "aabbccddeeff" in tokens


def test_includes_serial_and_vendor_and_classification() -> None:
    server = _server(
        identity=Identity(vendor=Vendor.DELL, serial="ABC1234", serial_normalized="abc1234"),
        classification={"installation_type": InstallationType.HOSTED_CLUSTER},
    )
    tokens = build_search_tokens(server)
    assert "abc1234" in tokens
    assert "dell" in tokens
    assert "hosted_cluster" in tokens


def test_tokens_shorter_than_min_length_are_dropped() -> None:
    tokens = build_search_tokens(_server(name="a-b-ocp"))
    assert "a" not in tokens
    assert "b" not in tokens
    assert "ocp" in tokens


def test_output_is_sorted_and_deterministic() -> None:
    server = _server(name="ocp-dell-worker-002")
    assert build_search_tokens(server) == build_search_tokens(server)
    assert build_search_tokens(server) == sorted(build_search_tokens(server))


def test_empty_server_produces_some_tokens_without_crashing() -> None:
    tokens = build_search_tokens(_server())
    assert isinstance(tokens, list)
    assert "ocp" in tokens  # from the default name in _server()
