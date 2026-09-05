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

from app.api.v1.classification_schemas import (
    ClassificationRuleListResponse,
    ClassificationRuleResponse,
    RuleFlagsSchema,
    RuleScopeSchema,
)
from app.application.services.audit_service import AuditService
from app.application.services.classification_service import (
    ClassificationService,
)
from app.config import Settings, get_settings
from app.dependencies import get_mongo_holder
from app.domain.models.classification_rule import RuleFlags, RuleScope
from app.domain.ports.regex_engine import RegexEngine
from app.domain.services.regex_engine import RegexModuleEngine
from app.errors import NotFoundError
from app.infrastructure.mongodb.audit_event_repository import MongoAuditEventRepository
from app.infrastructure.mongodb.classification_rule_repository import (
    MongoClassificationRuleRepository,
)
from app.infrastructure.mongodb.client import MongoClientHolder

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
