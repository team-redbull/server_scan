"""`GET /api/v1/sites` — the fixed site list and per-site statistics.

Sites are a closed enum (`app.domain.enums.SiteCode`), not rows a user
creates, so this endpoint always returns every one in a stable order
whether or not any server currently reports one. A site with zero servers
renders as an empty site, never as a missing card — "site four has
nothing in it" and "site four does not exist" are different facts and the
UI should be able to tell them apart.

The `unassigned` bucket at the end counts servers whose name carries no
site token (see `app.domain.value_objects.site`). It is deliberately
surfaced rather than hidden: a growing unassigned count is how a naming
drift becomes visible.
"""

from __future__ import annotations

from typing import Annotated

import structlog
from fastapi import APIRouter, Depends

from app.api.v1.sites_schemas import (
    SiteStats,
    SiteStatsListResponse,
    VendorCount,
)
from app.config import Settings, get_settings
from app.dependencies import get_mongo_holder, get_redis_holder
from app.domain.enums import HealthSeverity, Vendor
from app.domain.ports.repository import SiteBreakdownRow
from app.domain.value_objects.site import SiteCatalog, site_catalog
from app.infrastructure.mongodb.client import MongoClientHolder
from app.infrastructure.mongodb.server_repository import MongoServerRepository
from app.infrastructure.redis.cache import CacheClient
from app.infrastructure.redis.client import RedisClientHolder

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1", tags=["sites"])

# Short TTL, not a long one with explicit invalidation: `site_breakdown`
# is a full collection scan, and a collector run changes these numbers
# continuously, so there is no clean event to invalidate on. Thirty
# seconds keeps the landing page off the aggregation on every refresh
# while staying fresh enough that a maintenance toggle shows up on the
# next look — the same cache-aside, degrade-to-Mongo contract every other
# read path here follows.
_STATS_TTL_SECONDS = 30
_STATS_CACHE_KEY = "si:1:sites:stats"

# The key servers are counted under when their name carries no site.
UNASSIGNED_SITE_ID = "unassigned"

# Fixed presentation order for the per-vendor breakdown, so the three
# columns never reorder between renders.
_VENDOR_ORDER: tuple[str, ...] = tuple(v.value for v in Vendor)
_HEALTH_ORDER: tuple[str, ...] = tuple(s.value for s in HealthSeverity)


def _server_repo(
    mongo: Annotated[MongoClientHolder, Depends(get_mongo_holder)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> MongoServerRepository:
    return MongoServerRepository(mongo, cursor_secret=settings.cursor_secret)


def _cache(
    redis: Annotated[RedisClientHolder, Depends(get_redis_holder)],
) -> CacheClient:
    return CacheClient(redis)


def _empty_stats(site_id: str, *, name: str) -> SiteStats:
    return SiteStats(
        site_id=site_id,
        name=name,
        total=0,
        by_vendor=[VendorCount(vendor=vendor, count=0) for vendor in _VENDOR_ORDER],
        by_health=dict.fromkeys(_HEALTH_ORDER, 0),
        in_maintenance=0,
    )


def _pivot(rows: list[SiteBreakdownRow], sites: SiteCatalog) -> list[SiteStats]:
    """Fold the flat `$group` buckets into one record per site.

    Every configured site is seeded first so the shape of the response
    does not depend on what happens to be in the database — the UI can
    render a card per site without null-checking each one, and a site
    with no servers yet still appears.

    This endpoint is also the *only* place the frontend learns which
    sites exist, so reconfiguring `INVENTORY_SITES` reaches the UI with
    no frontend change at all.

    Args:
        rows (list[SiteBreakdownRow]): The aggregation's flat buckets.
        sites (SiteCatalog): The configured sites.

    Returns:
        list[SiteStats]: One record per site, plus "Unassigned".
    """
    stats: dict[str, SiteStats] = {
        definition.code: _empty_stats(definition.code, name=definition.name)
        for definition in sites.definitions
    }
    stats[UNASSIGNED_SITE_ID] = _empty_stats(UNASSIGNED_SITE_ID, name="Unassigned")

    vendor_index = {vendor: position for position, vendor in enumerate(_VENDOR_ORDER)}

    for row in rows:
        site_id = row.site_id or UNASSIGNED_SITE_ID
        entry = stats.get(site_id)
        if entry is None:
            # A site value that is no longer in the enum (data written by
            # an older schema). Counted under `unassigned` rather than
            # dropped, so the totals still add up to the real fleet size.
            entry = stats[UNASSIGNED_SITE_ID]

        entry.total += row.count
        if row.maintenance:
            entry.in_maintenance += row.count

        if row.health in entry.by_health:
            entry.by_health[row.health] += row.count
        else:
            entry.by_health[HealthSeverity.UNKNOWN.value] += row.count

        position = vendor_index.get(row.vendor or "")
        if position is not None:
            entry.by_vendor[position].count += row.count

    return list(stats.values())


@router.get("/sites", response_model=SiteStatsListResponse)
async def list_sites(
    repo: Annotated[MongoServerRepository, Depends(_server_repo)],
    cache: Annotated[CacheClient, Depends(_cache)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> SiteStatsListResponse:
    cached = await cache.get(_STATS_CACHE_KEY)
    if cached is not None:
        return SiteStatsListResponse.model_validate(cached)

    response = SiteStatsListResponse(
        items=_pivot(await repo.site_breakdown(), site_catalog(settings.sites))
    )
    await cache.set(
        _STATS_CACHE_KEY, response.model_dump(mode="json"), ttl_seconds=_STATS_TTL_SECONDS
    )
    return response
