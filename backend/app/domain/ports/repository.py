"""The `ServerRepository` port.

`domain/` declares this Protocol; `infrastructure/mongodb/` implements it.
Nothing in `application/` or `api/` talks to PyMongo directly — every
server read/write goes through this interface, which is what makes the
Mongo-specific cursor/query mechanics (`app.domain.services.search`,
`app.domain.services.cursor`) swappable in tests without a real database.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.domain.models.server import Server


@dataclass(frozen=True, slots=True)
class SiteBreakdownRow:
    """One `$group` bucket from `ServerRepository.site_breakdown`.

    Values are the raw stored strings, not enums: this is a count of what
    is actually in the database, including any value a previous schema
    wrote. The API layer decides how to present a value it doesn't
    recognize rather than this failing to decode it.
    """

    site_id: str | None
    vendor: str | None
    health: str | None
    maintenance: bool
    installation_type: str | None
    count: int


@dataclass(frozen=True, slots=True)
class Page:
    """One page of a keyset-paginated `Server` listing.

    `next_cursor` is an opaque, HMAC-signed string (see
    `app.domain.services.cursor`) — callers never construct or parse it,
    only pass it back verbatim to request the next page.
    """

    items: list[Server]
    next_cursor: str | None
    has_more: bool
    total_count: int | None  # only populated when the caller asked for it


class ServerRepository(Protocol):
    async def upsert(self, server: Server) -> Server:
        """Insert or update a server document by `_id`. Ingestion-owned
        fields overwrite; caller is responsible for not clobbering
        user-owned fields (tags/notes) — see the field-ownership note in
        `app.application.services.ingest`.
        """
        ...

    async def get_by_id(self, server_id: str) -> Server | None: ...

    async def list_page(
        self,
        *,
        filters: dict[str, object],
        search: str | None,
        sort: str,
        sort_desc: bool,
        cursor: str | None,
        page_size: int,
        with_count: bool,
    ) -> Page:
        """`filters` is already validated/whitelisted by the caller
        (`app.domain.services.search`) — this method trusts its keys are
        safe Mongo field paths, never raw user input.
        """
        ...

    async def count(self, filters: dict[str, object]) -> int: ...

    async def site_breakdown(self) -> list[SiteBreakdownRow]: ...
