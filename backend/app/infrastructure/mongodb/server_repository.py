"""MongoDB implementation of the `ServerRepository` port.

Keyset (not skip/limit) pagination throughout: `list_page` sorts on
`(sort_field, _id)` and, when a cursor is present, adds an `$or` clause
positioned just past `(cursor.sort_value, cursor.id_value)` rather than
using `.skip(n)`, which degrades linearly with offset and can return
duplicate/missing rows under concurrent writes. See
`app.domain.services.cursor` for the cursor's signing/binding contract and
`app.infrastructure.mongodb.indexes` for the compound indexes this leans
on to stay an IXSCAN at every page.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pymongo.asynchronous.collection import AsyncCollection

from app.domain.models.server import Server
from app.domain.ports.repository import Page, SiteBreakdownRow
from app.domain.services.cursor import CursorPosition, decode_cursor, encode_cursor
from app.domain.services.search import SORT_ACCESSORS, build_search_query, resolve_sort_field
from app.errors import NotFoundError, RevisionConflictError
from app.infrastructure.mongodb.client import MongoClientHolder
from app.infrastructure.mongodb.indexes import SERVERS_COLLECTION

_Document = dict[str, Any]


def _cursor_position_clause(
    *, sort_field: str, direction: int, position: CursorPosition
) -> dict[str, object]:
    """
    Build the `$or` clause selecting documents strictly past `position`.

    Either the sort field is strictly beyond the cursor's value, or it's
    tied and `_id` breaks the tie in the same direction. Both legs must
    agree with the query's own `.sort()` direction or the page would skip
    or repeat rows.

    Args:
        sort_field (str): The field being sorted on.
        direction (int): `1` for ascending, `-1` for descending.
        position (CursorPosition): The decoded cursor position.

    Returns:
        dict[str, object]: A Mongo filter clause to `$and` onto the query.
    """
    op = "$gt" if direction == 1 else "$lt"
    return {
        "$or": [
            {sort_field: {op: position.sort_value}},
            {"$and": [{sort_field: position.sort_value}, {"_id": {op: position.id_value}}]},
        ]
    }


@dataclass(frozen=True, slots=True)
class FacetRow:
    """
    One combination of filterable values and how many servers share it.

    Attributes:
        vendor (str | None): `identity.vendor`.
        source_provider (str | None): The collector that found them.
        installation_type (str | None): `classification.installation_type`.
        health_overall (str | None): `health.overall`.
        maintenance (bool): Whether maintenance is enabled.
        count (int): How many servers.
    """

    vendor: str | None
    source_provider: str | None
    installation_type: str | None
    health_overall: str | None
    maintenance: bool
    count: int


class MongoServerRepository:
    """
    Implements `app.domain.ports.repository.ServerRepository`.

    Structural typing (the Protocol has no `register()`/ABC to inherit
    from) means this class satisfies the port by matching its method
    signatures, not by subclassing it.
    """

    def __init__(self, mongo: MongoClientHolder, *, cursor_secret: str) -> None:
        """
        Store the shared Mongo client holder and the cursor-signing secret.

        Args:
            mongo (MongoClientHolder): The connected client holder.
            cursor_secret (str): HMAC secret used to sign/verify pagination
                cursors (see `app.domain.services.cursor`).
        """
        self._mongo = mongo
        self._cursor_secret = cursor_secret

    @property
    def _collection(self) -> AsyncCollection[_Document]:
        return self._mongo.db[SERVERS_COLLECTION]

    async def upsert(self, server: Server) -> Server:
        """
        Replace-or-insert a server by `_id`.

        Args:
            server (Server): The server to persist.

        Returns:
            Server: The same server, for chaining.

        Raises:
            pymongo.errors.DuplicateKeyError: If the document collides with
                an *other* document on the one secondary unique index,
                `(identity.vendor, identity.serial_normalized)` —
                uncaught; that is expected and is `app.application.
                services.ingest`'s job to catch and resolve via
                lookup+update, not this repository's. `identity.
                system_uuid` cannot raise this: it is indexed but not
                unique, since 2026-09-09 (see `app.infrastructure.
                mongodb.indexes`'s module docstring).
        """
        doc = server.model_dump(by_alias=True, mode="json")
        await self._collection.replace_one({"_id": server.id}, doc, upsert=True)
        return server

    async def upsert_with_revision_check(self, server: Server, *, expected_revision: int) -> Server:
        """
        Compare-and-set a server on its `revision`.

        The filter only matches the document a caller actually read
        (`_id` *and* the revision it saw), so a concurrent writer that
        already advanced the revision loses the race here instead of
        silently clobbering the other's write. No `upsert=True` — this
        never creates a document, so a filter that matches nothing is
        unconditionally a conflict, distinguished below by whether the
        document exists at all.

        Args:
            server (Server): The server to persist, as read plus changes.
            expected_revision (int): The revision the caller last read.

        Returns:
            Server: The same server, for chaining.

        Raises:
            NotFoundError: If no document with this `_id` exists at all.
            RevisionConflictError: If the document exists but its stored
                revision no longer matches `expected_revision`.
        """
        doc = server.model_dump(by_alias=True, mode="json")
        result = await self._collection.replace_one(
            {"_id": server.id, "revision": expected_revision}, doc
        )
        if result.matched_count == 0:
            current = await self._collection.find_one(
                {"_id": server.id}, projection={"revision": 1}
            )
            if current is None:
                raise NotFoundError(
                    f"No server with id {server.id!r}.", details={"server_id": server.id}
                )
            raise RevisionConflictError(
                f"Server {server.id!r} was modified concurrently: expected revision "
                f"{expected_revision}, stored revision is now {current['revision']}.",
                current_revision=current["revision"],
            )
        return server

    async def get_by_id(self, server_id: str) -> Server | None:
        """
        Look up one server by its id.

        Args:
            server_id (str): The server's id.

        Returns:
            Server | None: The server, or None if not found.
        """
        doc = await self._collection.find_one({"_id": server_id})
        if doc is None:
            return None
        return Server.model_validate(doc)

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
        """
        List one keyset page of servers matching filters/search.

        Args:
            filters (dict[str, object]): A Mongo filter document (already
                whitelisted by `app.domain.services.search.FILTER_FIELDS`).
            search (str | None): Free-text search string, or None.
            sort (str): The sort field name to resolve via
                `resolve_sort_field`.
            sort_desc (bool): Whether to sort descending.
            cursor (str | None): An opaque cursor from a previous page's
                `next_cursor`, or None for the first page.
            page_size (int): Maximum number of items to return.
            with_count (bool): Whether to also compute `total_count`
                (a separate `count_documents` round trip).

        Returns:
            Page: The matching items, the next cursor (`None` if this is
                the last page), whether more remain, and the total count
                if requested.
        """
        sort_field = resolve_sort_field(sort)
        direction = -1 if sort_desc else 1

        base_filter: dict[str, object] = dict(filters)
        if search:
            base_filter.update(build_search_query(search))

        query_filter: dict[str, object] = base_filter
        if cursor:
            position = decode_cursor(
                cursor,
                filters=filters,
                sort=sort,
                sort_desc=sort_desc,
                page_size=page_size,
                secret=self._cursor_secret,
            )
            cursor_clause = _cursor_position_clause(
                sort_field=sort_field, direction=direction, position=position
            )
            query_filter = {"$and": [base_filter, cursor_clause]} if base_filter else cursor_clause

        # Fetch one extra document to detect `has_more` without a second
        # round trip.
        raw_docs = await (
            self._collection.find(query_filter)
            .sort([(sort_field, direction), ("_id", direction)])
            .limit(page_size + 1)
            .to_list(length=page_size + 1)
        )

        has_more = len(raw_docs) > page_size
        items = [Server.model_validate(doc) for doc in raw_docs[:page_size]]

        next_cursor: str | None = None
        if has_more and items:
            last = items[-1]
            next_cursor = encode_cursor(
                sort_value=SORT_ACCESSORS[sort](last),
                id_value=last.id,
                filters=filters,
                sort=sort,
                sort_desc=sort_desc,
                page_size=page_size,
                secret=self._cursor_secret,
            )

        total_count: int | None = None
        if with_count:
            total_count = await self._collection.count_documents(base_filter)

        return Page(
            items=items, next_cursor=next_cursor, has_more=has_more, total_count=total_count
        )

    async def count(self, filters: dict[str, object]) -> int:
        """
        Count servers matching a Mongo filter document.

        Args:
            filters (dict[str, object]): The Mongo filter to count against.

        Returns:
            int: The number of matching servers.
        """
        return await self._collection.count_documents(dict(filters))

    async def facet_breakdown(
        self, *, filters: dict[str, object], search: str | None
    ) -> list[FacetRow]:
        """
        Per-combination server counts for one filtered view, in one round trip.

        A single `$group` over a composite key rather than a `$facet` with
        one sub-pipeline per dimension, for the same reason
        `site_breakdown` uses one: the key's cardinality is bounded by the
        enums and not by the estate — 4 vendors x 5 collectors x 3
        installation types x 5 severities x 2 maintenance states, so 600
        small rows at absolute worst — and each dimension's counts are the
        marginals the caller sums out of them. The alternative is a
        `count_documents` per option, which is one round trip per number
        on the screen.

        `site_id` is deliberately not part of the key. It is a filter an
        operator has usually already applied by the time they want these
        numbers, and including it would multiply the rows by the site
        count to answer a question the site overview already answers.

        Args:
            filters (dict[str, object]): The same Mongo filter document
                `list_page` receives, so the counts describe exactly the
                page being looked at.
            search (str | None): The same search string, applied the same
                way.

        Returns:
            list[FacetRow]: One row per non-empty combination.
        """
        match: dict[str, object] = dict(filters)
        if search:
            match.update(build_search_query(search))

        pipeline: list[dict[str, Any]] = []
        if match:
            pipeline.append({"$match": match})
        pipeline.append(
            {
                "$group": {
                    "_id": {
                        "vendor": "$identity.vendor",
                        "source_provider": "$source_provider",
                        "installation_type": "$classification.installation_type",
                        "health_overall": "$health.overall",
                        "maintenance": "$maintenance.enabled",
                    },
                    "count": {"$sum": 1},
                }
            }
        )

        rows: list[FacetRow] = []
        async for doc in await self._collection.aggregate(pipeline):
            key = doc["_id"]
            rows.append(
                FacetRow(
                    vendor=key.get("vendor"),
                    source_provider=key.get("source_provider"),
                    installation_type=key.get("installation_type"),
                    health_overall=key.get("health_overall"),
                    maintenance=bool(key.get("maintenance")),
                    count=int(doc["count"]),
                )
            )
        return rows

    async def site_breakdown(self) -> list[SiteBreakdownRow]:
        """
        Per-(site, vendor, health, maintenance, installation type) counts.

        Server counts for the whole estate, in one round trip. A single
        `$group` over every server rather than one count query
        per cell: the grouping key has a bounded cardinality (sites x 4
        vendors x 5 severities x 2 maintenance states x 3 installation
        types), so the four shipped sites plus the unassigned bucket give
        at most 600 small rows — and that stays bounded by the configured
        site count, never by the size of the estate. The caller pivots
        them in Python. The alternative — a `count_documents` per cell —
        would be that many round trips to build one screen.

        This is a full pass over the collection, which no index avoids for
        a grouping with no match stage. That is why the route in front of
        it caches: see `app.api.v1.sites`.

        Returns:
            list[SiteBreakdownRow]: One row per non-empty combination.
        """
        pipeline: list[dict[str, Any]] = [
            {
                "$group": {
                    "_id": {
                        "site_id": "$site_id",
                        "vendor": "$identity.vendor",
                        "health": "$health.overall",
                        "maintenance": "$maintenance.enabled",
                        "installation_type": "$classification.installation_type",
                    },
                    "count": {"$sum": 1},
                }
            }
        ]
        rows: list[SiteBreakdownRow] = []
        async for doc in await self._collection.aggregate(pipeline):
            key = doc["_id"]
            rows.append(
                SiteBreakdownRow(
                    site_id=key.get("site_id"),
                    vendor=key.get("vendor"),
                    health=key.get("health"),
                    maintenance=bool(key.get("maintenance")),
                    installation_type=key.get("installation_type"),
                    count=int(doc["count"]),
                )
            )
        return rows
