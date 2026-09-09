from datetime import UTC, datetime
from typing import Any

from app.domain.enums import InstallationType, Vendor
from app.domain.models.network import BmcInfo, NetworkInfo
from app.domain.models.server import Identity, Server
from app.domain.services.search_tokens import build_search_tokens

NOW = datetime.now(UTC)


def _finds(tokens: list[str], query: str) -> bool:
    """
    Whether the anchored-prefix query the API builds would match.

    Asserted instead of token literals: the token shape is an
    implementation detail, "typing this finds that" is the contract.

    Args:
        tokens (list[str]): One server's tokens.
        query (str): What an operator typed.

    Returns:
        bool: Whether any token starts with it.
    """
    return any(token.startswith(query.lower()) for token in tokens)


def _server(**overrides: Any) -> Server:
    defaults: dict[str, Any] = {
        "_id": "srv_test",
        "name": "ocp-dell-worker-001",
        # Required now that `Identity.vendor` has no default; overridable
        # by the tests that actually care about vendor.
        "identity": Identity(vendor=Vendor.DELL),
        "created_at": NOW,
        "updated_at": NOW,
    }
    defaults.update(overrides)
    return Server(**defaults)


def test_splits_hyphenated_name_into_tokens() -> None:
    tokens = build_search_tokens(_server(name="ocp-dell-worker-001"))
    assert "ocp-dell-worker-001" in tokens  # full value retained
    for part in ("ocp", "dell", "worker", "001"):
        assert _finds(tokens, part)


def test_a_fragment_from_the_middle_of_a_name_is_found() -> None:
    """The reason tokens are word-boundary suffixes rather than bare parts.

    Operators search by the part of a structured name they know (ADR-0025).
    """
    tokens = build_search_tokens(_server(name="ocp4-dok-five-compte-01"))
    for query in ("dok", "dok-five", "five-compte", "compte-01", "ocp4-dok"):
        assert _finds(tokens, query), query


def test_a_fragment_spanning_parts_is_found() -> None:
    tokens = build_search_tokens(_server(name="ocp-cisco-m6-bat-yam-128c-1024gb-CIS0000010"))
    for query in ("cisco-m6", "bat-yam", "128c-1024gb", "cis0000010"):
        assert _finds(tokens, query), query


def test_a_fragment_starting_mid_part_is_not_found() -> None:
    """The accepted limit of boundary suffixes, recorded so it is a choice.

    True substring matching costs ~650ms per facet query at 50k (ADR-0025).
    """
    tokens = build_search_tokens(_server(name="ocp4-dok-five-compte-01"))
    assert not _finds(tokens, "ok-five")


def test_includes_the_bmc_host() -> None:
    server = _server(network=NetworkInfo(bmc=BmcInfo(host="10.20.30.41")))
    tokens = build_search_tokens(server)
    assert "10.20.30.41" in tokens


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
    tokens = build_search_tokens(_server(name="ocp-a"))
    assert "a" not in tokens
    assert _finds(tokens, "ocp")


def test_output_is_sorted_and_deterministic() -> None:
    server = _server(name="ocp-dell-worker-002")
    assert build_search_tokens(server) == build_search_tokens(server)
    assert build_search_tokens(server) == sorted(build_search_tokens(server))


def test_empty_server_produces_some_tokens_without_crashing() -> None:
    tokens = build_search_tokens(_server())
    assert isinstance(tokens, list)
    assert _finds(tokens, "ocp")  # from the default name in _server()
