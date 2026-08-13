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

from app.api.v1.maintenance_schemas import MaintenanceEnableRequest
from app.api.v1.schemas import PageInfo, ServerDetail, ServerListResponse, ServerSummary
from app.application.services.audit_service import AuditService
from app.application.services.classification_service import ClassificationService
from app.application.services.health_policy_service import HealthPolicyService
from app.application.services.maintenance_service import MaintenanceService
from app.application.services.pipeline import classification_from_result, health_from_state
from app.config import Settings, get_settings
from app.dependencies import get_current_actor, get_mongo_holder, get_redis_holder, get_request_id
from app.domain.models.audit_event import Actor, EventType
from app.domain.ports.regex_engine import RegexEngine
from app.domain.services.classification import ClassifiableServer
from app.domain.services.health.metrics import build_default_registry
from app.domain.services.regex_engine import RegexModuleEngine
from app.domain.services.search import build_filter_query, resolve_sort_field
from app.errors import NotFoundError, PageSizeTooLargeError, ValidationAppError
from app.infrastructure.mongodb.audit_event_repository import MongoAuditEventRepository
from app.infrastructure.mongodb.classification_rule_repository import (
    MongoClassificationRuleRepository,
)
from app.infrastructure.mongodb.client import MongoClientHolder
from app.infrastructure.mongodb.health_policy_repository import MongoHealthPolicyRepository
from app.infrastructure.mongodb.server_repository import MongoServerRepository
from app.infrastructure.redis.cache import (
    LIST_PAGE_TTL_SECONDS,
    SERVER_DETAIL_TTL_SECONDS,
    CacheClient,
)
from app.infrastructure.redis.client import RedisClientHolder
from app.infrastructure.redis.keys import list_key, server_key
from app.utils.digest import stable_hash
from app.utils.timeutil import utcnow

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


_METRIC_REGISTRY = build_default_registry()


def _regex_engine(settings: Annotated[Settings, Depends(get_settings)]) -> RegexEngine:
    return RegexModuleEngine(
        max_pattern_length=settings.regex_max_pattern_length,
        match_timeout_seconds=settings.regex_match_timeout_seconds,
    )


def _classification_service(
    mongo: Annotated[MongoClientHolder, Depends(get_mongo_holder)],
    engine: Annotated[RegexEngine, Depends(_regex_engine)],
) -> ClassificationService:
    return ClassificationService(
        rule_repo=MongoClassificationRuleRepository(mongo), engine=engine, mongo=mongo
    )


def _health_policy_service(
    mongo: Annotated[MongoClientHolder, Depends(get_mongo_holder)],
    server_repo: Annotated[MongoServerRepository, Depends(_server_repo)],
) -> HealthPolicyService:
    return HealthPolicyService(
        policy_repo=MongoHealthPolicyRepository(mongo),
        registry=_METRIC_REGISTRY,
        server_repo=server_repo,
    )


def _audit_service(mongo: Annotated[MongoClientHolder, Depends(get_mongo_holder)]) -> AuditService:
    return AuditService(repo=MongoAuditEventRepository(mongo))


def _maintenance_service(
    server_repo: Annotated[MongoServerRepository, Depends(_server_repo)],
    audit: Annotated[AuditService, Depends(_audit_service)],
) -> MaintenanceService:
    return MaintenanceService(server_repo=server_repo, audit=audit)


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


async def _invalidate_detail_cache(server_id: str, cache: CacheClient) -> None:
    """`server_key` embeds `revision`, so bumping `revision` on write
    already makes the previous cache entry unreachable — but the pointer
    entry (`_revision_pointer_key`) still points at the old revision until
    it expires on its own TTL, which would cost one extra (harmless, but
    avoidable) Mongo round trip on the very next `GET`. Deleting it here
    means the next read goes straight to the new revision's key.
    """
    await cache.delete(_revision_pointer_key(server_id))


@router.post("/servers/{server_id}/reclassify", response_model=ServerDetail)
async def reclassify_server(
    server_id: str,
    repo: Annotated[MongoServerRepository, Depends(_server_repo)],
    cache: Annotated[CacheClient, Depends(_cache_client)],
    service: Annotated[ClassificationService, Depends(_classification_service)],
    audit: Annotated[AuditService, Depends(_audit_service)],
    actor: Annotated[Actor, Depends(get_current_actor)],
    request_id: Annotated[str | None, Depends(get_request_id)],
) -> ServerDetail:
    """Re-runs the classification engine against this server's current
    identity fields and the *current* ruleset, and persists the result —
    the same classification step ingestion runs automatically, exposed
    here so editing a rule can be followed by "show me the effect on this
    server" without waiting for the server's next ingest cycle.
    """
    server = await repo.get_by_id(server_id)
    if server is None:
        raise NotFoundError(f"No server with id {server_id!r}.", details={"server_id": server_id})

    previous_type = server.classification.installation_type
    classifiable = ClassifiableServer(
        name=server.name,
        vendor=server.identity.vendor,
        manager_type=None,  # Server carries no manager_type field today
        site_id=server.site_id,
        serial=server.identity.serial,
        model=server.model,
    )
    result = await service.classify_server(classifiable)
    server.classification = classification_from_result(
        result, previous_version=server.classification.classification_version
    )
    server.revision += 1
    server.updated_at = utcnow()

    await repo.upsert(server)
    await _invalidate_detail_cache(server_id, cache)

    if server.classification.installation_type != previous_type:
        await audit.record(
            EventType.CLASSIFICATION_CHANGED,
            actor=actor,
            server_id=server_id,
            request_id=request_id,
            data={
                "from": previous_type.value,
                "to": server.classification.installation_type.value,
                "matched_rule_id": server.classification.matched_rule_id,
            },
        )
    return ServerDetail.from_server(server)


@router.post("/servers/{server_id}/health/recalculate", response_model=ServerDetail)
async def recalculate_server_health(
    server_id: str,
    repo: Annotated[MongoServerRepository, Depends(_server_repo)],
    cache: Annotated[CacheClient, Depends(_cache_client)],
    service: Annotated[HealthPolicyService, Depends(_health_policy_service)],
    audit: Annotated[AuditService, Depends(_audit_service)],
    actor: Annotated[Actor, Depends(get_current_actor)],
    request_id: Annotated[str | None, Depends(get_request_id)],
) -> ServerDetail:
    """Re-runs the health policy engine against this server's current
    facts and the *current* policy set, and persists the result. Same
    rationale as `reclassify_server`: proves "I edited a threshold, did
    this server's health change" without waiting for its next ingest.
    """
    server = await repo.get_by_id(server_id)
    if server is None:
        raise NotFoundError(f"No server with id {server_id!r}.", details={"server_id": server_id})

    previous_overall = server.health.overall
    state = await service.evaluate_server(server)
    server.health = health_from_state(state)
    server.revision += 1
    server.updated_at = utcnow()

    await repo.upsert(server)
    await _invalidate_detail_cache(server_id, cache)

    if server.health.overall != previous_overall:
        await audit.record(
            EventType.HEALTH_STATUS_CHANGED,
            actor=actor,
            server_id=server_id,
            request_id=request_id,
            data={
                "from": previous_overall.value,
                "to": server.health.overall.value,
                "policy_ids": [e.policy_id for e in state.evaluations if e.active],
            },
        )
    return ServerDetail.from_server(server)


@router.put("/servers/{server_id}/maintenance", response_model=ServerDetail)
async def enable_maintenance(
    server_id: str,
    payload: MaintenanceEnableRequest,
    service: Annotated[MaintenanceService, Depends(_maintenance_service)],
    cache: Annotated[CacheClient, Depends(_cache_client)],
    actor: Annotated[Actor, Depends(get_current_actor)],
    request_id: Annotated[str | None, Depends(get_request_id)],
) -> ServerDetail:
    server = await service.enable(
        server_id,
        reason=payload.reason,
        ticket=payload.ticket,
        expected_end=payload.expected_end,
        actor=actor,
        request_id=request_id,
    )
    await _invalidate_detail_cache(server_id, cache)
    return ServerDetail.from_server(server)


@router.delete("/servers/{server_id}/maintenance", response_model=ServerDetail)
async def disable_maintenance(
    server_id: str,
    service: Annotated[MaintenanceService, Depends(_maintenance_service)],
    cache: Annotated[CacheClient, Depends(_cache_client)],
    actor: Annotated[Actor, Depends(get_current_actor)],
    request_id: Annotated[str | None, Depends(get_request_id)],
) -> ServerDetail:
    server = await service.disable(server_id, actor=actor, request_id=request_id)
    await _invalidate_detail_cache(server_id, cache)
    return ServerDetail.from_server(server)
