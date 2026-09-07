"""`GET /api/v1/events`, `GET /api/v1/servers/{server_id}/events`.

Read-only by design: there is deliberately no `POST /events` — the only
way an event is created is a side effect of a real mutation
(`app.application.services.audit_service.AuditService.record`, called
from the services that own each mutation), never a direct API write. That
is what makes "the audit log reflects what actually happened" true instead
of "the audit log reflects what someone claimed happened."
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.v1.events_schemas import AuditEventListResponse, AuditEventResponse, EventPageInfo
from app.dependencies import get_mongo_holder
from app.infrastructure.mongodb.audit_event_repository import (
    AuditEventPage,
    MongoAuditEventRepository,
)
from app.infrastructure.mongodb.client import MongoClientHolder

router = APIRouter(prefix="/api/v1", tags=["events"])

_DEFAULT_PAGE_SIZE = 50
_MAX_PAGE_SIZE = 200


async def _event_repo(
    mongo: Annotated[MongoClientHolder, Depends(get_mongo_holder)],
) -> MongoAuditEventRepository:
    """
    Build the audit event repository for one request.

    Args:
        mongo (MongoClientHolder): The shared Mongo client holder.

    Returns:
        MongoAuditEventRepository: A repository bound to that client.
    """
    return MongoAuditEventRepository(mongo)


def _to_response(page: AuditEventPage, *, page_size: int) -> AuditEventListResponse:
    return AuditEventListResponse(
        items=[AuditEventResponse.model_validate(e.model_dump()) for e in page.items],
        page=EventPageInfo(
            next_cursor=page.next_cursor, has_more=page.has_more, page_size=page_size
        ),
    )


@router.get("/events", response_model=AuditEventListResponse)
async def list_events(
    repo: Annotated[MongoAuditEventRepository, Depends(_event_repo)],
    server_id: str | None = Query(default=None),
    event_type: str | None = Query(default=None),
    actor_id: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    page_size: int = Query(default=_DEFAULT_PAGE_SIZE, ge=1, le=_MAX_PAGE_SIZE),
) -> AuditEventListResponse:
    """
    List audit events across every server, newest first, with keyset paging.

    Args:
        repo (MongoAuditEventRepository): The audit event repository.
        server_id (str | None): Restrict to events for this server.
        event_type (str | None): Restrict to this event type.
        actor_id (str | None): Restrict to events recorded by this actor.
        cursor (str | None): Opaque cursor from a previous page's response.
        page_size (int): Maximum items to return, 1-200.

    Returns:
        AuditEventListResponse: The matching page of events.
    """
    page = await repo.list_page(
        server_id=server_id,
        event_type=event_type,
        actor_id=actor_id,
        cursor=cursor,
        page_size=page_size,
    )
    return _to_response(page, page_size=page_size)


@router.get("/servers/{server_id}/events", response_model=AuditEventListResponse)
async def list_server_events(
    server_id: str,
    repo: Annotated[MongoAuditEventRepository, Depends(_event_repo)],
    cursor: str | None = Query(default=None),
    page_size: int = Query(default=_DEFAULT_PAGE_SIZE, ge=1, le=_MAX_PAGE_SIZE),
) -> AuditEventListResponse:
    """
    List audit events for one server, newest first, with keyset paging.

    Args:
        server_id (str): The server's ID.
        repo (MongoAuditEventRepository): The audit event repository.
        cursor (str | None): Opaque cursor from a previous page's response.
        page_size (int): Maximum items to return, 1-200.

    Returns:
        AuditEventListResponse: The matching page of events.
    """
    page = await repo.list_page(server_id=server_id, cursor=cursor, page_size=page_size)
    return _to_response(page, page_size=page_size)
