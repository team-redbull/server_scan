"""Integration test for the full ingest pipeline: `FakeProvider` ->
`IngestService` -> `MongoServerRepository`/`MongoSiteRepository`/
`MongoManagerRepository`, against the live dev MongoDB.
"""

from __future__ import annotations

import pytest

from app.application.services.ingest import IngestService
from app.domain.enums import Vendor
from app.infrastructure.mongodb import MongoClientHolder
from app.infrastructure.mongodb.manager_repository import MongoManagerRepository
from app.infrastructure.mongodb.server_repository import MongoServerRepository
from app.infrastructure.mongodb.site_repository import MongoSiteRepository
from app.infrastructure.providers.fake.generator import list_managers, list_sites
from app.infrastructure.providers.fake.provider import FakeProvider

pytestmark = pytest.mark.integration

_CURSOR_SECRET = "test-cursor-secret"


def _service(mongo_holder: MongoClientHolder) -> IngestService:
    return IngestService(
        server_repo=MongoServerRepository(mongo_holder, cursor_secret=_CURSOR_SECRET),
        site_repo=MongoSiteRepository(mongo_holder),
        manager_repo=MongoManagerRepository(mongo_holder),
    )


async def test_ingest_creates_expected_number_of_servers(mongo_holder: MongoClientHolder) -> None:
    service = _service(mongo_holder)
    provider = FakeProvider(seed=7, count=40)

    summary = await service.ingest(provider, sites=list_sites(), managers=list_managers())

    assert summary.fetched == 40
    assert summary.created == 40
    assert summary.updated == 0
    assert summary.errors == 0

    server_repo = MongoServerRepository(mongo_holder, cursor_secret=_CURSOR_SECRET)
    count = await server_repo.count({})
    assert count == 40


async def test_reingest_same_seed_updates_not_duplicates(mongo_holder: MongoClientHolder) -> None:
    service = _service(mongo_holder)
    provider = FakeProvider(seed=11, count=20)

    first = await service.ingest(provider, sites=list_sites(), managers=list_managers())
    assert first.created == 20

    second = await service.ingest(provider, sites=list_sites(), managers=list_managers())
    assert second.fetched == 20
    assert second.created == 0
    assert second.updated == 20
    assert second.errors == 0

    server_repo = MongoServerRepository(mongo_holder, cursor_secret=_CURSOR_SECRET)
    assert await server_repo.count({}) == 20


async def test_ingested_sites_and_managers_are_upserted(mongo_holder: MongoClientHolder) -> None:
    service = _service(mongo_holder)
    provider = FakeProvider(seed=3, count=10)

    await service.ingest(provider, sites=list_sites(), managers=list_managers())

    site_repo = MongoSiteRepository(mongo_holder)
    manager_repo = MongoManagerRepository(mongo_holder)
    sites = await site_repo.list_all()
    managers = await manager_repo.list_all()

    assert len(sites) == len(list_sites())
    assert len(managers) == len(list_managers())


async def test_ingested_servers_are_searchable_and_filterable(
    mongo_holder: MongoClientHolder,
) -> None:
    service = _service(mongo_holder)
    provider = FakeProvider(seed=99, count=60)

    await service.ingest(provider, sites=list_sites(), managers=list_managers())

    server_repo = MongoServerRepository(mongo_holder, cursor_secret=_CURSOR_SECRET)
    dell_page = await server_repo.list_page(
        filters={"identity.vendor": Vendor.DELL.value},
        search=None,
        sort="name",
        sort_desc=False,
        cursor=None,
        page_size=100,
        with_count=True,
    )
    assert dell_page.total_count is not None
    assert dell_page.total_count > 0
    assert all(s.identity.vendor == Vendor.DELL for s in dell_page.items)
