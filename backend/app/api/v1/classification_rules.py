"""`/api/v1/classification-rules`: read-only listing and lookup.

No cursor pagination on `GET /classification-rules` — same rationale as
`MongoClassificationRuleRepository`'s docstring: this is a small,
human-curated collection (dozens to low hundreds of rules), not the
10k+-row `servers` collection `app.api.v1.servers` paginates.

Rules are read-only (`27b20a8`, `f9ab059`): they ship with the platform
and are seeded/validated at startup
(`app.application.services.bootstrap`, via
`app.application.services.classification_service.validate_rule_write`),
never created or edited through this router.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.v1.classification_schemas import (
    ClassificationRuleListResponse,
    ClassificationRuleResponse,
)
from app.dependencies import get_mongo_holder
from app.errors import NotFoundError
from app.infrastructure.mongodb.classification_rule_repository import (
    MongoClassificationRuleRepository,
)
from app.infrastructure.mongodb.client import MongoClientHolder

router = APIRouter(prefix="/api/v1/classification-rules", tags=["classification-rules"])


async def _rule_repo(
    mongo: Annotated[MongoClientHolder, Depends(get_mongo_holder)],
) -> MongoClassificationRuleRepository:
    return MongoClassificationRuleRepository(mongo)


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
