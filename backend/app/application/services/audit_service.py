"""`AuditService`: the one path through which an audit event is ever
recorded.

No other code in this codebase constructs an `AuditEvent` or calls
`MongoAuditEventRepository.record()` directly — every mutation that needs
an audit trail (classification rule CRUD, health policy CRUD,
classification/health changes, maintenance changes) goes through
`AuditService.record()`, so the id-generation, timestamping, and "which
repository method actually persists this" decisions live in exactly one
place.
"""

from __future__ import annotations

from typing import Any

from app.domain.models.audit_event import Actor, ActorType, AuditEvent, EventType
from app.infrastructure.mongodb.audit_event_repository import MongoAuditEventRepository
from app.utils.ids import new_id
from app.utils.timeutil import utcnow


class AuditService:
    def __init__(self, *, repo: MongoAuditEventRepository) -> None:
        self._repo = repo

    async def record(
        self,
        event_type: EventType,
        *,
        actor: Actor,
        server_id: str | None = None,
        request_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> AuditEvent:
        event = AuditEvent(
            id=new_id("event"),
            event_type=event_type,
            server_id=server_id,
            actor=actor,
            request_id=request_id,
            created_at=utcnow(),
            data=data or {},
        )
        return await self._repo.record(event)


# The system actor used for events emitted by background/automated code
# paths (ingestion, scheduled re-evaluation) rather than an interactive API
# request — see `app.application.services.ingest`'s health-status-changed
# emission. A stable, well-known id rather than one generated per call, so
# "everything the ingest pipeline has ever done" is a single
# `actor.id`-filtered query away.
SYSTEM_INGEST_ACTOR = Actor(type=ActorType.SYSTEM, id="ingestion")
