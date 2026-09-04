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

from typing import Any

from pymongo.asynchronous.collection import AsyncCollection

from app.domain.models.server import Server
from app.domain.ports.repository import Page, SiteBreakdownRow
from app.domain.services.cursor import CursorPosition, decode_cursor, encode_cursor
from app.domain.services.search import SORT_ACCESSORS, build_search_query, resolve_sort_field
from app.infrastructure.mongodb.client import MongoClientHolder
from app.infrastructure.mongodb.indexes import SERVERS_COLLECTION

_Document = dict[str, Any]


def _cursor_position_clause(
    *, sort_field: str, direction: int, position: CursorPosition
) -> dict[str, object]:
    """`$or` clause selecting documents strictly past `position` in sort
    order: either the sort field is strictly beyond the cursor's value, or
    it's tied and `_id` breaks the tie in the same direction. Both legs
    must agree with the query's own `.sort()` direction or the page would
    skip or repeat rows.
    """
    op = "$gt" if direction == 1 else "$lt"
    return {
        "$or": [
            {sort_field: {op: position.sort_value}},
            {"$and": [{sort_field: position.sort_value}, {"_id": {op: position.id_value}}]},
        ]
    }


class MongoServerRepository:
    """Implements `app.domain.ports.repository.ServerRepository` against
    the `servers` collection. Structural typing (the Protocol has no
    `register()`/ABC to inherit from) means this class satisfies the port
    by matching its method signatures, not by subclassing it.
    """

    def __init__(self, mongo: MongoClientHolder, *, cursor_secret: str) -> None:
        self._mongo = mongo
        self._cursor_secret = cursor_secret

    @property
    def _collection(self) -> AsyncCollection[_Document]:
        return self._mongo.db[SERVERS_COLLECTION]

    async def upsert(self, server: Server) -> Server:
        """Replace-or-insert by `_id`. Raises `pymongo.errors.
        DuplicateKeyError` (uncaught) if the document collides with an
        *other* document on a secondary unique index
        (`identity.system_uuid` or `(identity.vendor,
        identity.serial_normalized)`) — that is expected and is
        `app.application.services.ingest`'s job to catch and resolve via
        lookup+update, not this repository's.
        """
        doc = server.model_dump(by_alias=True, mode="json")
        await self._collection.replace_one({"_id": server.id}, doc, upsert=True)
        return server

    async def get_by_id(self, server_id: str) -> Server | None:
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
        return await self._collection.count_documents(dict(filters))

    async def site_breakdown(self) -> list[SiteBreakdownRow]:
        """Per (site, vendor, health, maintenance, installation type)
        server counts for the whole estate, in one round trip.

        A single `$group` over every server rather than one count query
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
