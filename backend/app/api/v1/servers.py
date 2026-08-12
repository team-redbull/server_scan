"""`GET /api/v1/servers`, `GET /api/v1/servers/{server_id}`.

Filters are deliberately *not* individual typed FastAPI query parameters:
FastAPI silently drops any query param that isn't bound to a declared
function parameter, which would make it impossible to ever reach
`UNKNOWN_FILTER` — the whole point of that error code is to reject a
caller-supplied filter key that isn't in the whitelist, not to ignore it.
So every query param *except* the fixed non-filter set (`search`, `sort`,
`sort_desc`, `cursor`, `page_size`, `with_count`) is collected generically
from `request.query_params` and run through
`app.domain.services.search.build_filter_query`, which is the single
place that knows the whitelist and raises `UnknownFilterError` for
anything outside it.

Caching: list pages are cache-aside under `list_key(...)` — the key is
fully computable from the request itself (filter/search/sort/cursor hash),
so there's no bootstrapping problem. Server detail is trickier: the key
design in `app.infrastructure.redis.keys.server_key` embeds the document's
`revision` specifically so a write never needs an explicit invalidation
call, but that means the *current* revision has to be known before the
real cache key can even be built — and the closed `ServerRepository`
port (`app.domain.ports.repository`, out of this slice's scope to modify)
has no cheap revision-only lookup, only a full `get_by_id`. This module
resolves that with a small self-maintained pointer entry
(`_revision_pointer_key`, id -> current revision, same TTL as the detail
payload) written through the same `CacheClient` — a normal cache-aside
read still degrades to Mongo on any Redis failure, it just costs one
extra (also-degrading) cache read on the hot path.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from app.api.v1.schemas import PageInfo, ServerDetail, ServerListResponse, ServerSummary
from app.config import Settings, get_settings
from app.dependencies import get_mongo_holder, get_redis_holder
from app.domain.services.search import build_filter_query, resolve_sort_field
from app.errors import NotFoundError, PageSizeTooLargeError, ValidationAppError
from app.infrastructure.mongodb.client import MongoClientHolder
from app.infrastructure.mongodb.server_repository import MongoServerRepository
from app.infrastructure.redis.cache import (
    LIST_PAGE_TTL_SECONDS,
    SERVER_DETAIL_TTL_SECONDS,
    CacheClient,
)
from app.infrastructure.redis.client import RedisClientHolder
from app.infrastructure.redis.keys import list_key, server_key
from app.utils.digest import stable_hash

router = APIRouter(prefix="/api/v1", tags=["servers"])

# Query params handled explicitly by `list_servers`; everything else in
# the query string is a candidate filter key (see module docstring).
_NON_FILTER_PARAMS = frozenset({"search", "sort", "sort_desc", "cursor", "page_size", "with_count"})

_TRUE_STRINGS = frozenset({"true", "1", "yes"})
_FALSE_STRINGS = frozenset({"false", "0", "no"})


def _parse_bool(raw: str, *, field: str) -> bool:
    lowered = raw.strip().lower()
    if lowered in _TRUE_STRINGS:
        return True
    if lowered in _FALSE_STRINGS:
        return False
    raise ValidationAppError(
        f"Query parameter {field!r} must be a boolean.", details={"field": field, "value": raw}
    )


def _extract_raw_filters(request: Request) -> dict[str, object]:
    filters: dict[str, object] = {}
    for key, value in request.query_params.items():
        if key in _NON_FILTER_PARAMS:
            continue
        if key == "maintenance":
            filters[key] = _parse_bool(value, field="maintenance")
        else:
            filters[key] = value
    return filters


def _server_repo(
    mongo: Annotated[MongoClientHolder, Depends(get_mongo_holder)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> MongoServerRepository:
    return MongoServerRepository(mongo, cursor_secret=settings.cursor_secret)


def _cache_client(redis: Annotated[RedisClientHolder, Depends(get_redis_holder)]) -> CacheClient:
    return CacheClient(redis)


def _revision_pointer_key(server_id: str) -> str:
    """Self-maintained cache entry mapping `server_id` -> its current
    `revision`, so `server_key(id, revision)` can be looked up without a
    full Mongo read first. See module docstring.
    """
    return f"si:1:srv:{server_id}:rev"


@router.get("/servers", response_model=ServerListResponse)
async def list_servers(
    request: Request,
    repo: Annotated[MongoServerRepository, Depends(_server_repo)],
    cache: Annotated[CacheClient, Depends(_cache_client)],
    settings: Annotated[Settings, Depends(get_settings)],
    search: str | None = Query(default=None),
    sort: str = Query(default="name"),
    sort_desc: bool = Query(default=False),
    cursor: str | None = Query(default=None),
    page_size: int | None = Query(default=None, ge=1),
    with_count: bool = Query(default=False),
) -> ServerListResponse:
    effective_page_size = page_size if page_size is not None else settings.default_page_size
    if effective_page_size > settings.max_page_size:
        raise PageSizeTooLargeError(
            f"page_size must not exceed {settings.max_page_size}.",
            details={"max_page_size": settings.max_page_size, "page_size": effective_page_size},
        )

    raw_filters = _extract_raw_filters(request)
    # Validate eagerly (unknown filter / unknown sort) before touching the
    # cache or Mongo — a request that can never succeed shouldn't cost a
    # Redis round trip first.
    mongo_filters = build_filter_query(raw_filters)
    resolve_sort_field(sort)  # fail fast on an unknown sort before any I/O

    cache_key = list_key(
        stable_hash(
            {
                "filters": mongo_filters,
                "search": search,
                "sort": sort,
                "sort_desc": sort_desc,
                "page_size": effective_page_size,
                "with_count": with_count,
            }
        ),
        stable_hash({"cursor": cursor}),
    )

    cached = await cache.get(cache_key)
    if cached is not None:
        return ServerListResponse.model_validate(cached)

    page = await repo.list_page(
        filters=mongo_filters,
        search=search,
        sort=sort,
        sort_desc=sort_desc,
        cursor=cursor,
        page_size=effective_page_size,
        with_count=with_count,
    )

    response = ServerListResponse(
        items=[ServerSummary.from_server(server) for server in page.items],
        page=PageInfo(
            next_cursor=page.next_cursor,
            has_more=page.has_more,
            page_size=effective_page_size,
            count=page.total_count,
            count_capped=False,
        ),
    )

    await cache.set(cache_key, response.model_dump(mode="json"), ttl_seconds=LIST_PAGE_TTL_SECONDS)
    return response


@router.get("/servers/{server_id}", response_model=ServerDetail)
async def get_server(
    server_id: str,
    repo: Annotated[MongoServerRepository, Depends(_server_repo)],
    cache: Annotated[CacheClient, Depends(_cache_client)],
) -> ServerDetail:
    pointer_key = _revision_pointer_key(server_id)
    cached_revision = await cache.get(pointer_key)
    if isinstance(cached_revision, int):
        cached_detail = await cache.get(server_key(server_id, cached_revision))
        if cached_detail is not None:
            return ServerDetail.model_validate(cached_detail)

    server = await repo.get_by_id(server_id)
    if server is None:
        raise NotFoundError(f"No server with id {server_id!r}.", details={"server_id": server_id})

    detail = ServerDetail.from_server(server)
    detail_key = server_key(server_id, server.revision)
    await cache.set(pointer_key, server.revision, ttl_seconds=SERVER_DETAIL_TTL_SECONDS)
    await cache.set(
        detail_key, detail.model_dump(mode="json"), ttl_seconds=SERVER_DETAIL_TTL_SECONDS
    )
    return detail
