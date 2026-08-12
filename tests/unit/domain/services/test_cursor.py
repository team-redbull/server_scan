from datetime import UTC, datetime

import pytest

from app.domain.services.cursor import decode_cursor, encode_cursor
from app.errors import CursorFilterMismatchError, CursorInvalidError

SECRET = "test-secret"
FILTERS: dict[str, object] = {"identity.vendor": "dell"}
SORT = "name"
SORT_DESC = False
PAGE_SIZE = 50


def _encode(**overrides: object) -> str:
    defaults: dict[str, object] = {
        "sort_value": "ocp-dell-worker-001",
        "id_value": "srv_abc123",
        "filters": FILTERS,
        "sort": SORT,
        "sort_desc": SORT_DESC,
        "page_size": PAGE_SIZE,
        "secret": SECRET,
    }
    defaults.update(overrides)
    return encode_cursor(**defaults)  # type: ignore[arg-type]


def test_round_trip_string_sort_value() -> None:
    cursor = _encode()
    position = decode_cursor(
        cursor, filters=FILTERS, sort=SORT, sort_desc=SORT_DESC, page_size=PAGE_SIZE, secret=SECRET
    )
    assert position.sort_value == "ocp-dell-worker-001"
    assert position.id_value == "srv_abc123"


def test_round_trip_datetime_sort_value() -> None:
    now = datetime(2026, 8, 12, 12, 0, 0, tzinfo=UTC)
    cursor = _encode(sort_value=now)
    position = decode_cursor(
        cursor, filters=FILTERS, sort=SORT, sort_desc=SORT_DESC, page_size=PAGE_SIZE, secret=SECRET
    )
    assert position.sort_value == now


def test_tampered_signature_rejected() -> None:
    cursor = _encode()
    payload_b64, signature_b64 = cursor.split(".", 1)
    tampered = f"{payload_b64}.{signature_b64[:-1]}{'A' if signature_b64[-1] != 'A' else 'B'}"
    with pytest.raises(CursorInvalidError):
        decode_cursor(
            tampered,
            filters=FILTERS,
            sort=SORT,
            sort_desc=SORT_DESC,
            page_size=PAGE_SIZE,
            secret=SECRET,
        )


def test_tampered_payload_rejected() -> None:
    cursor = _encode()
    payload_b64, signature_b64 = cursor.split(".", 1)
    tampered_payload = f"{payload_b64}AAAA.{signature_b64}"
    with pytest.raises(CursorInvalidError):
        decode_cursor(
            tampered_payload,
            filters=FILTERS,
            sort=SORT,
            sort_desc=SORT_DESC,
            page_size=PAGE_SIZE,
            secret=SECRET,
        )


def test_wrong_secret_rejected() -> None:
    cursor = _encode()
    with pytest.raises(CursorInvalidError):
        decode_cursor(
            cursor,
            filters=FILTERS,
            sort=SORT,
            sort_desc=SORT_DESC,
            page_size=PAGE_SIZE,
            secret="a-different-secret",
        )


def test_filter_mismatch_rejected() -> None:
    cursor = _encode()
    with pytest.raises(CursorFilterMismatchError):
        decode_cursor(
            cursor,
            filters={"identity.vendor": "cisco"},
            sort=SORT,
            sort_desc=SORT_DESC,
            page_size=PAGE_SIZE,
            secret=SECRET,
        )


def test_sort_change_is_a_filter_mismatch() -> None:
    cursor = _encode()
    with pytest.raises(CursorFilterMismatchError):
        decode_cursor(
            cursor,
            filters=FILTERS,
            sort="updated_at",
            sort_desc=SORT_DESC,
            page_size=PAGE_SIZE,
            secret=SECRET,
        )


def test_page_size_change_is_a_filter_mismatch() -> None:
    cursor = _encode()
    with pytest.raises(CursorFilterMismatchError):
        decode_cursor(
            cursor,
            filters=FILTERS,
            sort=SORT,
            sort_desc=SORT_DESC,
            page_size=25,
            secret=SECRET,
        )


@pytest.mark.parametrize(
    "malformed",
    [
        "",
        "not-a-cursor-at-all",
        "onlyonepart",
        "!!!not-base64!!!.also-not-base64!!!",
        "....",
    ],
)
def test_malformed_cursor_rejected(malformed: str) -> None:
    with pytest.raises(CursorInvalidError):
        decode_cursor(
            malformed,
            filters=FILTERS,
            sort=SORT,
            sort_desc=SORT_DESC,
            page_size=PAGE_SIZE,
            secret=SECRET,
        )
