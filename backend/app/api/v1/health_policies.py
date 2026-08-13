"""`/api/v1/health-policies` CRUD + preview, and `/api/v1/health-metrics`.

Follows the same thin-router pattern as `app.api.v1.servers`: dependency
providers construct repositories/services from what's already on
`app.state` (never a module-level global holder), routes stay free of
business logic, and every domain-facing error is an `AppError` subclass —
never a raw `HTTPException` — so it renders through the shared RFC 9457
handler in `app.exception_handlers`.

The metric registry is built once at import time (`_METRIC_REGISTRY`)
rather than per-request or off `app.state`: `build_default_registry()` is
a pure, deterministic function over a fixed set of ~11 core metric
definitions (see `app.domain.services.health.metrics`'s own docstring on
why it's constructed explicitly rather than kept as global mutable
state) — a module-level constant here is that same "construct once,
pass explicitly" discipline, just scoped to this router instead of
`app.main`'s lifespan, since nothing about it depends on settings or a
live connection the way Mongo/Redis holders do.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import ValidationError as PydanticValidationError

from app.api.v1.health_policy_schemas import (
    HealthMetricListResponse,
    HealthMetricResponse,
    HealthPolicyCreate,
    HealthPolicyListResponse,
    HealthPolicyPreviewRequest,
    HealthPolicyPreviewResponse,
    HealthPolicyPreviewSample,
    HealthPolicyResponse,
    HealthPolicyUpdate,
)
from app.application.services.audit_service import AuditService
from app.application.services.health_policy_service import (
    HealthPolicyService,
    validate_policy_write,
    validate_system_field_lock,
)
from app.config import Settings, get_settings
from app.dependencies import get_current_actor, get_mongo_holder, get_request_id
from app.domain.models.audit_event import Actor, EventType
from app.domain.models.health_policy import HealthPolicy
from app.domain.services.health.metrics import MetricRegistry, build_default_registry
from app.errors import ConflictError, NotFoundError, ValidationAppError
from app.infrastructure.mongodb.audit_event_repository import MongoAuditEventRepository
from app.infrastructure.mongodb.client import MongoClientHolder
from app.infrastructure.mongodb.health_policy_repository import MongoHealthPolicyRepository
from app.infrastructure.mongodb.server_repository import MongoServerRepository
from app.utils.ids import new_id
from app.utils.timeutil import utcnow

router = APIRouter(prefix="/api/v1", tags=["health-policies"])

_METRIC_REGISTRY = build_default_registry()


def _metric_registry() -> MetricRegistry:
    return _METRIC_REGISTRY


def _policy_repo(
    mongo: Annotated[MongoClientHolder, Depends(get_mongo_holder)],
) -> MongoHealthPolicyRepository:
    return MongoHealthPolicyRepository(mongo)


def _server_repo(
    mongo: Annotated[MongoClientHolder, Depends(get_mongo_holder)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> MongoServerRepository:
    return MongoServerRepository(mongo, cursor_secret=settings.cursor_secret)


def _health_policy_service(
    policy_repo: Annotated[MongoHealthPolicyRepository, Depends(_policy_repo)],
    server_repo: Annotated[MongoServerRepository, Depends(_server_repo)],
    registry: Annotated[MetricRegistry, Depends(_metric_registry)],
) -> HealthPolicyService:
    return HealthPolicyService(policy_repo=policy_repo, registry=registry, server_repo=server_repo)


def _audit_service(mongo: Annotated[MongoClientHolder, Depends(get_mongo_holder)]) -> AuditService:
    return AuditService(repo=MongoAuditEventRepository(mongo))


def _validate_and_build(payload: dict[str, Any]) -> HealthPolicy:
    """Shared create/update tail: turn a raw dict into a validated
    `HealthPolicy`, converting a raw pydantic `ValidationError` into a
    client-facing `ValidationAppError` — a bare pydantic `ValidationError`
    must never reach a client directly (see `app.exception_handlers`'s
    module docstring: every error renders as an RFC 9457 problem-details
    body, and only `AppError` subclasses and FastAPI's own
    `RequestValidationError` are wired to do that).
    """
    try:
        return HealthPolicy.model_validate(payload)
    except PydanticValidationError as exc:
        # The default `.errors()` embeds, per error, the raw input value
        # (for a model-level validator — priority-band, mode validity —
        # that's the *entire* payload, including `datetime` fields) and a
        # `ctx` dict that can carry the raised `ValueError` object itself
        # — neither is JSON-serializable, and `JSONResponse` has no
        # fallback encoder. `type`/`loc`/`msg` are enough for a client to
        # act on without echoing back non-JSON-safe internals.
        raise ValidationAppError(
            "Health policy failed validation.",
            details={
                "errors": exc.errors(include_input=False, include_url=False, include_context=False)
            },
        ) from exc


@router.get("/health-policies", response_model=HealthPolicyListResponse)
async def list_policies(
    policy_repo: Annotated[MongoHealthPolicyRepository, Depends(_policy_repo)],
    enabled: bool | None = Query(default=None),
) -> HealthPolicyListResponse:
    policies = await policy_repo.list_all(enabled_only=bool(enabled))
    return HealthPolicyListResponse(items=[HealthPolicyResponse.from_policy(p) for p in policies])


@router.post("/health-policies", response_model=HealthPolicyResponse, status_code=201)
async def create_policy(
    body: HealthPolicyCreate,
    policy_repo: Annotated[MongoHealthPolicyRepository, Depends(_policy_repo)],
    registry: Annotated[MetricRegistry, Depends(_metric_registry)],
    audit: Annotated[AuditService, Depends(_audit_service)],
    actor: Annotated[Actor, Depends(get_current_actor)],
    request_id: Annotated[str | None, Depends(get_request_id)],
) -> HealthPolicyResponse:
    if await policy_repo.get_by_name(body.name) is not None:
        raise ConflictError(
            f"A health policy named {body.name!r} already exists.", details={"name": body.name}
        )

    policy_id = new_id("health_policy")
    now = utcnow()
    payload = body.model_dump(by_alias=True)
    payload["_id"] = policy_id
    payload["policy_key"] = body.policy_key or policy_id
    payload["system"] = False
    payload["created_at"] = now
    payload["updated_at"] = now

    policy = _validate_and_build(payload)
    validate_policy_write(policy, registry=registry)

    await policy_repo.upsert(policy)
    await audit.record(
        EventType.HEALTH_POLICY_CREATED,
        actor=actor,
        request_id=request_id,
        data={"policy_id": policy.id, "name": policy.name, "policy_key": policy.policy_key},
    )
    return HealthPolicyResponse.from_policy(policy)


@router.get("/health-policies/{policy_id}", response_model=HealthPolicyResponse)
async def get_policy(
    policy_id: str,
    policy_repo: Annotated[MongoHealthPolicyRepository, Depends(_policy_repo)],
) -> HealthPolicyResponse:
    policy = await policy_repo.get_by_id(policy_id)
    if policy is None:
        raise NotFoundError(
            f"No health policy with id {policy_id!r}.", details={"policy_id": policy_id}
        )
    return HealthPolicyResponse.from_policy(policy)


@router.patch("/health-policies/{policy_id}", response_model=HealthPolicyResponse)
async def update_policy(
    policy_id: str,
    body: HealthPolicyUpdate,
    policy_repo: Annotated[MongoHealthPolicyRepository, Depends(_policy_repo)],
    registry: Annotated[MetricRegistry, Depends(_metric_registry)],
    audit: Annotated[AuditService, Depends(_audit_service)],
    actor: Annotated[Actor, Depends(get_current_actor)],
    request_id: Annotated[str | None, Depends(get_request_id)],
) -> HealthPolicyResponse:
    existing = await policy_repo.get_by_id(policy_id)
    if existing is None:
        raise NotFoundError(
            f"No health policy with id {policy_id!r}.", details={"policy_id": policy_id}
        )

    updates = body.model_dump(exclude_unset=True, by_alias=True)
    validate_system_field_lock(existing=existing, updates=updates)

    if "name" in updates and updates["name"] != existing.name:
        name_conflict = await policy_repo.get_by_name(updates["name"])
        if name_conflict is not None and name_conflict.id != existing.id:
            raise ConflictError(
                f"A health policy named {updates['name']!r} already exists.",
                details={"name": updates["name"]},
            )

    merged_payload = existing.model_dump(by_alias=True)
    merged_payload.update(updates)
    merged_payload["revision"] = existing.revision + 1
    merged_payload["updated_at"] = utcnow()

    merged = _validate_and_build(merged_payload)
    validate_policy_write(merged, registry=registry)

    await policy_repo.upsert(merged)

    # A transition from enabled to disabled is the specific, actionable
    # event ("this policy stopped protecting servers") — everything else
    # about the update is the generic UPDATED event.
    just_disabled = existing.enabled and not merged.enabled
    await audit.record(
        EventType.HEALTH_POLICY_DISABLED if just_disabled else EventType.HEALTH_POLICY_UPDATED,
        actor=actor,
        request_id=request_id,
        data={"policy_id": merged.id, "changed_fields": sorted(updates)},
    )
    return HealthPolicyResponse.from_policy(merged)


@router.delete("/health-policies/{policy_id}", status_code=204)
async def delete_policy(
    policy_id: str,
    policy_repo: Annotated[MongoHealthPolicyRepository, Depends(_policy_repo)],
    audit: Annotated[AuditService, Depends(_audit_service)],
    actor: Annotated[Actor, Depends(get_current_actor)],
    request_id: Annotated[str | None, Depends(get_request_id)],
) -> None:
    existing = await policy_repo.get_by_id(policy_id)
    if existing is None:
        raise NotFoundError(
            f"No health policy with id {policy_id!r}.", details={"policy_id": policy_id}
        )
    if existing.system:
        raise ValidationAppError(
            "System health policies cannot be deleted.", details={"policy_id": policy_id}
        )
    await policy_repo.delete(policy_id)
    await audit.record(
        EventType.HEALTH_POLICY_DELETED,
        actor=actor,
        request_id=request_id,
        data={"policy_id": policy_id, "name": existing.name},
    )


@router.post("/health-policies/preview", response_model=HealthPolicyPreviewResponse)
async def preview_policy(
    body: HealthPolicyPreviewRequest,
    service: Annotated[HealthPolicyService, Depends(_health_policy_service)],
) -> HealthPolicyPreviewResponse:
    payload = body.model_dump(by_alias=True)
    sample_size = payload.pop("sample_size")
    max_scan = payload.pop("max_scan")
    policy_id = payload.pop("policy_id", None)
    if policy_id is not None:
        payload["_id"] = policy_id

    result = await service.preview(payload, sample_size=sample_size, max_scan=max_scan)
    return HealthPolicyPreviewResponse(
        matched_count=result.matched_count,
        truncated=result.truncated,
        sample=[
            HealthPolicyPreviewSample(id=m.id, name=m.name, would_be_severity=m.would_be_severity)
            for m in result.sample
        ],
        mode=result.mode,
    )


@router.get("/health-metrics", response_model=HealthMetricListResponse, tags=["health-metrics"])
async def list_health_metrics(
    registry: Annotated[MetricRegistry, Depends(_metric_registry)],
) -> HealthMetricListResponse:
    return HealthMetricListResponse(
        items=[
            HealthMetricResponse(
                name=m.name,
                type=m.type,
                category=m.category,
                description=m.description,
                enum_values=list(m.enum_values) if m.enum_values is not None else None,
                provider=m.provider,
            )
            for m in registry.all()
        ]
    )
