"""`/api/v1/classification-rules` CRUD + preview.

No cursor pagination on `GET /classification-rules` — same rationale as
`MongoClassificationRuleRepository`'s docstring: this is a small,
human-curated collection (dozens to low hundreds of rules), not the
10k+-row `servers` collection `app.api.v1.servers` paginates.

Thin handlers throughout: cross-field validation lives in
`app.application.services.classification_service.validate_rule_write`,
never duplicated here.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pymongo.errors import DuplicateKeyError

from app.api.v1.classification_schemas import (
    ClassificationPreviewRequest,
    ClassificationPreviewResponse,
    ClassificationRuleCreate,
    ClassificationRuleListResponse,
    ClassificationRuleResponse,
    ClassificationRuleUpdate,
    RuleFlagsSchema,
    RuleScopeSchema,
)
from app.application.services.audit_service import AuditService
from app.application.services.classification_service import (
    ClassificationService,
    validate_rule_write,
)
from app.config import Settings, get_settings
from app.dependencies import get_current_actor, get_mongo_holder, get_request_id
from app.domain.models.audit_event import Actor, EventType
from app.domain.models.classification_rule import ClassificationRule, RuleFlags, RuleScope
from app.domain.ports.regex_engine import RegexEngine
from app.domain.services.regex_engine import RegexModuleEngine
from app.errors import ConflictError, NotFoundError, ValidationAppError
from app.infrastructure.mongodb.audit_event_repository import MongoAuditEventRepository
from app.infrastructure.mongodb.classification_rule_repository import (
    MongoClassificationRuleRepository,
)
from app.infrastructure.mongodb.client import MongoClientHolder
from app.utils.ids import new_id
from app.utils.timeutil import utcnow

router = APIRouter(prefix="/api/v1/classification-rules", tags=["classification-rules"])


def _rule_repo(
    mongo: Annotated[MongoClientHolder, Depends(get_mongo_holder)],
) -> MongoClassificationRuleRepository:
    return MongoClassificationRuleRepository(mongo)


def _regex_engine(settings: Annotated[Settings, Depends(get_settings)]) -> RegexEngine:
    return RegexModuleEngine(
        max_pattern_length=settings.regex_max_pattern_length,
        match_timeout_seconds=settings.regex_match_timeout_seconds,
    )


def _classification_service(
    mongo: Annotated[MongoClientHolder, Depends(get_mongo_holder)],
    rule_repo: Annotated[MongoClassificationRuleRepository, Depends(_rule_repo)],
    engine: Annotated[RegexEngine, Depends(_regex_engine)],
) -> ClassificationService:
    return ClassificationService(rule_repo=rule_repo, engine=engine, mongo=mongo)


def _scope_from_schema(scope: RuleScopeSchema) -> RuleScope:
    return RuleScope(vendor=scope.vendor, manager_type=scope.manager_type, site_id=scope.site_id)


def _flags_from_schema(flags: RuleFlagsSchema) -> RuleFlags:
    return RuleFlags(ignore_case=flags.ignore_case, multiline=flags.multiline, dotall=flags.dotall)


def _audit_service(mongo: Annotated[MongoClientHolder, Depends(get_mongo_holder)]) -> AuditService:
    return AuditService(repo=MongoAuditEventRepository(mongo))


@router.get("", response_model=ClassificationRuleListResponse)
async def list_rules(
    repo: Annotated[MongoClassificationRuleRepository, Depends(_rule_repo)],
    enabled: bool | None = Query(default=None),
) -> ClassificationRuleListResponse:
    if enabled is None:
        rules = await repo.list_all()
    elif enabled:
        rules = await repo.list_all(enabled_only=True)
    else:
        rules = [r for r in await repo.list_all() if not r.enabled]
    return ClassificationRuleListResponse(
        items=[ClassificationRuleResponse.from_rule(r) for r in rules]
    )


@router.post("", response_model=ClassificationRuleResponse, status_code=201)
async def create_rule(
    payload: ClassificationRuleCreate,
    repo: Annotated[MongoClassificationRuleRepository, Depends(_rule_repo)],
    engine: Annotated[RegexEngine, Depends(_regex_engine)],
    audit: Annotated[AuditService, Depends(_audit_service)],
    actor: Annotated[Actor, Depends(get_current_actor)],
    request_id: Annotated[str | None, Depends(get_request_id)],
) -> ClassificationRuleResponse:
    now = utcnow()
    rule = ClassificationRule(
        id=new_id("classification_rule"),
        name=payload.name,
        description=payload.description,
        enabled=payload.enabled,
        system=False,
        installation_type=payload.installation_type,
        scope=_scope_from_schema(payload.scope),
        field=payload.field,
        pattern=payload.pattern,
        flags=_flags_from_schema(payload.flags),
        source=payload.source,
        priority=payload.priority,
        order=payload.order,
        created_at=now,
        updated_at=now,
    )
    validate_rule_write(rule, engine, is_create=True)

    try:
        await repo.upsert(rule)
    except DuplicateKeyError as exc:
        raise ConflictError(
            f"A classification rule named {rule.name!r} already exists.",
            details={"name": rule.name},
        ) from exc

    await audit.record(
        EventType.CLASSIFICATION_RULE_CREATED,
        actor=actor,
        request_id=request_id,
        data={
            "rule_id": rule.id,
            "name": rule.name,
            "installation_type": rule.installation_type.value,
        },
    )
    return ClassificationRuleResponse.from_rule(rule)


@router.get("/{rule_id}", response_model=ClassificationRuleResponse)
async def get_rule(
    rule_id: str,
    repo: Annotated[MongoClassificationRuleRepository, Depends(_rule_repo)],
) -> ClassificationRuleResponse:
    rule = await repo.get_by_id(rule_id)
    if rule is None:
        raise NotFoundError(
            f"No classification rule with id {rule_id!r}.", details={"rule_id": rule_id}
        )
    return ClassificationRuleResponse.from_rule(rule)


@router.patch("/{rule_id}", response_model=ClassificationRuleResponse)
async def update_rule(
    rule_id: str,
    payload: ClassificationRuleUpdate,
    repo: Annotated[MongoClassificationRuleRepository, Depends(_rule_repo)],
    engine: Annotated[RegexEngine, Depends(_regex_engine)],
    audit: Annotated[AuditService, Depends(_audit_service)],
    actor: Annotated[Actor, Depends(get_current_actor)],
    request_id: Annotated[str | None, Depends(get_request_id)],
) -> ClassificationRuleResponse:
    existing = await repo.get_by_id(rule_id)
    if existing is None:
        raise NotFoundError(
            f"No classification rule with id {rule_id!r}.", details={"rule_id": rule_id}
        )

    update_fields = payload.model_dump(exclude_unset=True)

    if existing.system:
        disallowed = set(update_fields) - {"enabled"}
        if disallowed:
            raise ValidationAppError(
                "System classification rules can only have `enabled` changed.",
                details={"rule_id": rule_id, "disallowed_fields": sorted(disallowed)},
            )

    merged = existing.model_copy(deep=True)
    if "name" in update_fields:
        merged.name = update_fields["name"]
    if "description" in update_fields:
        merged.description = update_fields["description"]
    if "enabled" in update_fields:
        merged.enabled = update_fields["enabled"]
    if "installation_type" in update_fields:
        merged.installation_type = update_fields["installation_type"]
    if "scope" in update_fields:
        scope_in = update_fields["scope"] or {}
        merged.scope = RuleScope(
            vendor=scope_in.get("vendor"),
            manager_type=scope_in.get("manager_type"),
            site_id=scope_in.get("site_id"),
        )
    if "field" in update_fields:
        merged.field = update_fields["field"]
    if "pattern" in update_fields:
        merged.pattern = update_fields["pattern"]
    if "flags" in update_fields:
        flags_in = update_fields["flags"] or {}
        merged.flags = RuleFlags(
            ignore_case=flags_in.get("ignore_case", True),
            multiline=flags_in.get("multiline", False),
            dotall=flags_in.get("dotall", False),
        )
    if "source" in update_fields:
        merged.source = update_fields["source"]
    if "priority" in update_fields:
        merged.priority = update_fields["priority"]
    if "order" in update_fields:
        merged.order = update_fields["order"]

    merged.revision = existing.revision + 1
    merged.updated_at = utcnow()

    validate_rule_write(merged, engine, is_create=False)

    try:
        await repo.upsert(merged)
    except DuplicateKeyError as exc:
        raise ConflictError(
            f"A classification rule named {merged.name!r} already exists.",
            details={"name": merged.name},
        ) from exc

    await audit.record(
        EventType.CLASSIFICATION_RULE_UPDATED,
        actor=actor,
        request_id=request_id,
        data={"rule_id": merged.id, "changed_fields": sorted(update_fields)},
    )
    return ClassificationRuleResponse.from_rule(merged)


@router.delete("/{rule_id}", status_code=204)
async def delete_rule(
    rule_id: str,
    repo: Annotated[MongoClassificationRuleRepository, Depends(_rule_repo)],
    audit: Annotated[AuditService, Depends(_audit_service)],
    actor: Annotated[Actor, Depends(get_current_actor)],
    request_id: Annotated[str | None, Depends(get_request_id)],
) -> None:
    existing = await repo.get_by_id(rule_id)
    if existing is None:
        raise NotFoundError(
            f"No classification rule with id {rule_id!r}.", details={"rule_id": rule_id}
        )
    if existing.system:
        raise ValidationAppError(
            "System classification rules cannot be deleted; disable them instead.",
            details={"rule_id": rule_id},
        )
    await repo.delete(rule_id)
    await audit.record(
        EventType.CLASSIFICATION_RULE_DELETED,
        actor=actor,
        request_id=request_id,
        data={"rule_id": rule_id, "name": existing.name},
    )


@router.post("/preview", response_model=ClassificationPreviewResponse)
async def preview_rule(
    payload: ClassificationPreviewRequest,
    service: Annotated[ClassificationService, Depends(_classification_service)],
) -> ClassificationPreviewResponse:
    result = await service.preview(payload.model_dump(mode="json"))
    return ClassificationPreviewResponse(
        matched_count=result.matched_count,
        truncated=result.truncated,
        sample=result.sample,
        mode=result.mode,
    )
