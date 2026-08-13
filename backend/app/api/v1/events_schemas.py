"""`GET /api/v1/events`, `GET /api/v1/servers/{server_id}/events` schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel

from app.domain.models.audit_event import Actor, EventType


class AuditEventResponse(BaseModel):
    id: str
    event_type: EventType
    server_id: str | None
    actor: Actor
    request_id: str | None
    created_at: datetime
    data: dict[str, Any]


class EventPageInfo(BaseModel):
    next_cursor: str | None
    has_more: bool
    page_size: int


class AuditEventListResponse(BaseModel):
    items: list[AuditEventResponse]
    page: EventPageInfo
