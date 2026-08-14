"""Integration tests for `MongoServerRepository` against the live dev
MongoDB (see `tests/integration/conftest.py` for the skip-if-unreachable
fixture). Covers insert/get round-trip, unique-index enforcement, keyset
pagination correctness, filters, search, sort direction, and — the actual
point of the `search_tokens` index design — that the search query is an
IXSCAN, not a COLLSCAN.
"""

from __future__ import annotations

import json

import pytest
from pymongo.errors import DuplicateKeyError

from app.domain.enums import HealthSeverity, InstallationType, Vendor
from app.domain.models.classification import Classification
from app.domain.models.health import Health
from app.domain.models.maintenance import Maintenance
from app.domain.models.server import Identity, Server
from app.domain.services.normalize import normalize_text
from app.domain.services.search import build_search_query
from app.domain.services.search_tokens import build_search_tokens
from app.errors import CursorFilterMismatchError, CursorInvalidError, UnknownSortFieldError
from app.infrastructure.mongodb import MongoClientHolder
from app.infrastructure.mongodb.server_repository import MongoServerRepository
from app.utils.ids import new_id
from app.utils.timeutil import utcnow

pytestmark = pytest.mark.integration

_CURSOR_SECRET = "test-cursor-secret"


def _make_server(
    index: int,
    *,
    site_id: str | None = None,
    vendor: Vendor = Vendor.DELL,
    health: HealthSeverity = HealthSeverity.UNKNOWN,
    installation_type: InstallationType = InstallationType.UNCLASSIFIED,
    maintenance_enabled: bool = False,
    name: str | None = None,
    serial: str | None = None,
    system_uuid: str | None = None,
) -> Server:
    now = utcnow()
    nm = name if name is not None else f"srv-{index:04d}"
    ser = serial if serial is not None else f"SN{index:06d}"
    uid = system_uuid if system_uuid is not None else f"uuid-{index:06d}"
    server = Server(
        _id=new_id("server"),
        name=nm,
        name_normalized=normalize_text(nm),
        identity=Identity(
            vendor=vendor, serial=ser, serial_normalized=normalize_text(ser), system_uuid=uid
        ),
        site_id=site_id,
        classification=Classification(installation_type=installation_type),
        health=Health(overall=health),
        maintenance=Maintenance(enabled=maintenance_enabled),
        created_at=now,
        updated_at=now,
        last_seen_at=now,
    )
    server.search_tokens = build_search_tokens(server)
    return server


async def test_insert_and_get_round_trip(mongo_holder: MongoClientHolder) -> None:
    repo = MongoServerRepository(mongo_holder, cursor_secret=_CURSOR_SECRET)
    server = _make_server(1)

    await repo.upsert(server)
    fetched = await repo.get_by_id(server.id)

    assert fetched is not None
    assert fetched.id == server.id
    assert fetched.name == server.name
    assert fetched.identity.serial == server.identity.serial


async def test_get_by_id_missing_returns_none(mongo_holder: MongoClientHolder) -> None:
    repo = MongoServerRepository(mongo_holder, cursor_secret=_CURSOR_SECRET)
    assert await repo.get_by_id("srv_does_not_exist") is None


async def test_duplicate_system_uuid_raises_duplicate_key_error(
    mongo_holder: MongoClientHolder,
) -> None:
    repo = MongoServerRepository(mongo_holder, cursor_secret=_CURSOR_SECRET)
    first = _make_server(1, system_uuid="uuid-shared")
    second = _make_server(2, system_uuid="uuid-shared")

    await repo.upsert(first)
    with pytest.raises(DuplicateKeyError):
        await repo.upsert(second)


async def test_duplicate_vendor_serial_raises_duplicate_key_error(
    mongo_holder: MongoClientHolder,
) -> None:
    repo = MongoServerRepository(mongo_holder, cursor_secret=_CURSOR_SECRET)
    first = _make_server(1, vendor=Vendor.DELL, serial="SHARED123", system_uuid="uuid-a")
    second = _make_server(2, vendor=Vendor.DELL, serial="SHARED123", system_uuid="uuid-b")

    await repo.upsert(first)
    with pytest.raises(DuplicateKeyError):
        await repo.upsert(second)


async def test_pagination_covers_every_document_exactly_once(
    mongo_holder: MongoClientHolder,
) -> None:
    repo = MongoServerRepository(mongo_holder, cursor_secret=_CURSOR_SECRET)
    total = 53
    for i in range(total):
        await repo.upsert(_make_server(i))

    seen: list[str] = []
    cursor: str | None = None
    page_size = 10
    for _ in range(total):  # generous upper bound on iterations
        page = await repo.list_page(
            filters={},
            search=None,
            sort="name",
            sort_desc=False,
            cursor=cursor,
            page_size=page_size,
            with_count=False,
        )
        seen.extend(item.id for item in page.items)
        if not page.has_more:
            break
        cursor = page.next_cursor
        assert cursor is not None

    assert len(seen) == total
    assert len(set(seen)) == total  # no duplicates


async def test_filters_narrow_results(mongo_holder: MongoClientHolder) -> None:
    repo = MongoServerRepository(mongo_holder, cursor_secret=_CURSOR_SECRET)
    for i in range(5):
        await repo.upsert(_make_server(i, site_id="one"))
    for i in range(5, 8):
        await repo.upsert(_make_server(i, site_id="two"))

    page = await repo.list_page(
        filters={"site_id": "one"},
        search=None,
        sort="name",
        sort_desc=False,
        cursor=None,
        page_size=50,
        with_count=True,
    )

    assert len(page.items) == 5
    assert all(item.site_id == "one" for item in page.items)
    assert page.total_count == 5


async def test_search_matches_by_token(mongo_holder: MongoClientHolder) -> None:
    repo = MongoServerRepository(mongo_holder, cursor_secret=_CURSOR_SECRET)
    await repo.upsert(_make_server(1, name="ocp-dell-worker-001"))
    await repo.upsert(_make_server(2, name="upi-cisco-master-002"))
    await repo.upsert(_make_server(3, name="random-server-0003"))

    page = await repo.list_page(
        filters={},
        search="ocp",
        sort="name",
        sort_desc=False,
        cursor=None,
        page_size=50,
        with_count=False,
    )

    assert len(page.items) == 1
    assert page.items[0].name == "ocp-dell-worker-001"


async def test_sort_ascending_and_descending(mongo_holder: MongoClientHolder) -> None:
    repo = MongoServerRepository(mongo_holder, cursor_secret=_CURSOR_SECRET)
    names = ["bravo", "alpha", "charlie"]
    for i, name in enumerate(names):
        await repo.upsert(_make_server(i, name=name))

    ascending = await repo.list_page(
        filters={},
        search=None,
        sort="name",
        sort_desc=False,
        cursor=None,
        page_size=50,
        with_count=False,
    )
    descending = await repo.list_page(
        filters={},
        search=None,
        sort="name",
        sort_desc=True,
        cursor=None,
        page_size=50,
        with_count=False,
    )

    assert [s.name for s in ascending.items] == ["alpha", "bravo", "charlie"]
    assert [s.name for s in descending.items] == ["charlie", "bravo", "alpha"]


async def test_unknown_sort_field_raises(mongo_holder: MongoClientHolder) -> None:
    repo = MongoServerRepository(mongo_holder, cursor_secret=_CURSOR_SECRET)
    with pytest.raises(UnknownSortFieldError):
        await repo.list_page(
            filters={},
            search=None,
            sort="not_a_real_sort",
            sort_desc=False,
            cursor=None,
            page_size=10,
            with_count=False,
        )


async def test_stale_cursor_after_filter_change_is_rejected(
    mongo_holder: MongoClientHolder,
) -> None:
    repo = MongoServerRepository(mongo_holder, cursor_secret=_CURSOR_SECRET)
    for i in range(5):
        await repo.upsert(_make_server(i, site_id="one"))

    page = await repo.list_page(
        filters={"site_id": "one"},
        search=None,
        sort="name",
        sort_desc=False,
        cursor=None,
        page_size=2,
        with_count=False,
    )
    assert page.next_cursor is not None

    with pytest.raises(CursorFilterMismatchError):
        await repo.list_page(
            filters={"site_id": "two"},
            search=None,
            sort="name",
            sort_desc=False,
            cursor=page.next_cursor,
            page_size=2,
            with_count=False,
        )


async def test_garbage_cursor_is_rejected(mongo_holder: MongoClientHolder) -> None:
    repo = MongoServerRepository(mongo_holder, cursor_secret=_CURSOR_SECRET)
    with pytest.raises(CursorInvalidError):
        await repo.list_page(
            filters={},
            search=None,
            sort="name",
            sort_desc=False,
            cursor="not-a-real-cursor",
            page_size=10,
            with_count=False,
        )


async def test_count_reflects_filters(mongo_holder: MongoClientHolder) -> None:
    repo = MongoServerRepository(mongo_holder, cursor_secret=_CURSOR_SECRET)
    for i in range(4):
        await repo.upsert(_make_server(i, health=HealthSeverity.CRITICAL))
    for i in range(4, 6):
        await repo.upsert(_make_server(i, health=HealthSeverity.HEALTHY))

    count = await repo.count({"health.overall": HealthSeverity.CRITICAL.value})
    assert count == 4


async def test_search_query_uses_index_scan_not_collection_scan(
    mongo_holder: MongoClientHolder,
) -> None:
    """The entire point of the `search_tokens` multikey index: an anchored,
    escaped-prefix search must be an IXSCAN, never a COLLSCAN, at any
    collection size a real deployment could reach.
    """
    repo = MongoServerRepository(mongo_holder, cursor_secret=_CURSOR_SECRET)
    for i in range(30):
        await repo.upsert(_make_server(i, name=f"ocp-dell-worker-{i:03d}"))

    mongo_query = build_search_query("ocp-dell")
    explain = await mongo_holder.db["servers"].find(mongo_query).explain()
    explain_str = json.dumps(explain)

    assert "COLLSCAN" not in explain_str
    assert "IXSCAN" in explain_str
