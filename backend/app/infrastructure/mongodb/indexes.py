"""Declarative MongoDB index definitions.

Indexes are declared here, as data, rather than issued as ad-hoc
`create_index` calls scattered through repository code — `ensure_indexes`
is called once per process at startup (see `app.main`'s lifespan) and is
safe to call every time: `create_indexes` is idempotent when an index
already exists with an identical spec, and raises loudly
(`OperationFailure`) if an existing index's spec has actually drifted from
what's declared here, which is a schema-drift bug we want surfaced
immediately rather than silently ignored.

Every compound index on `servers` ends in `_id` (ascending or descending
to match the leading field's sort direction) specifically to support
keyset pagination (`app.domain.services.cursor`): a `(filter_field,
sort_field, _id)` index lets `list_page`'s `$or` cursor-position query and
`.sort([(sort_field, dir), ("_id", dir)])` both use the same index instead
of falling back to an in-memory sort past the 32MB blocking-sort limit.

The two unique partial indexes enforce identity constraints at the
database layer (not just in application code) precisely because slice 2's
identity-correlation ladder doesn't exist yet — until it does, "insert
what looks like a new server" is the only path ingestion has, and a
collision on `system_uuid` or `(vendor, serial_normalized)` must be
rejected loudly (`DuplicateKeyError`) rather than silently creating a
duplicate document for the same physical machine.
"""

from __future__ import annotations

from typing import Any

from pymongo import ASCENDING, DESCENDING, IndexModel
from pymongo.asynchronous.database import AsyncDatabase

SERVERS_COLLECTION = "servers"
SITES_COLLECTION = "sites"
MANAGERS_COLLECTION = "managers"

SERVER_INDEXES: list[IndexModel] = [
    IndexModel(
        [("identity.system_uuid", ASCENDING)],
        name="uniq_system_uuid",
        unique=True,
        partialFilterExpression={"identity.system_uuid": {"$exists": True}},
    ),
    IndexModel(
        [("identity.vendor", ASCENDING), ("identity.serial_normalized", ASCENDING)],
        name="uniq_vendor_serial",
        unique=True,
        partialFilterExpression={"identity.serial_normalized": {"$ne": ""}},
    ),
    # Multikey — backs `app.domain.services.search.build_search_query`'s
    # anchored-prefix regex match.
    IndexModel([("search_tokens", ASCENDING)], name="search_tokens"),
    # One compound index per filter whitelisted in
    # `app.domain.services.search.FILTER_FIELDS`, each ending in the
    # default sort field + `_id` so "filter by X, sorted by name" is a
    # single IXSCAN for the common case.
    IndexModel(
        [("site_id", ASCENDING), ("name_normalized", ASCENDING), ("_id", ASCENDING)],
        name="site_name_id",
    ),
    IndexModel(
        [("health.overall", ASCENDING), ("name_normalized", ASCENDING), ("_id", ASCENDING)],
        name="health_name_id",
    ),
    IndexModel(
        [
            ("classification.installation_type", ASCENDING),
            ("name_normalized", ASCENDING),
            ("_id", ASCENDING),
        ],
        name="installation_type_name_id",
    ),
    IndexModel(
        [("identity.vendor", ASCENDING), ("name_normalized", ASCENDING), ("_id", ASCENDING)],
        name="vendor_name_id",
    ),
    IndexModel([("updated_at", DESCENDING), ("_id", DESCENDING)], name="updated_at_id"),
    IndexModel([("last_seen_at", ASCENDING)], name="last_seen_at"),
    # Unfiltered sorts (no `FILTER_FIELDS` value supplied) still need a
    # supporting index per `SORT_FIELDS` entry, or they fall back to an
    # in-memory sort.
    IndexModel([("name_normalized", ASCENDING), ("_id", ASCENDING)], name="name_id"),
    IndexModel([("identity.serial_normalized", ASCENDING), ("_id", ASCENDING)], name="serial_id"),
    IndexModel([("model_normalized", ASCENDING), ("_id", ASCENDING)], name="model_id"),
]

SITE_INDEXES: list[IndexModel] = [
    IndexModel([("name", ASCENDING)], name="uniq_name", unique=True),
]

MANAGER_INDEXES: list[IndexModel] = [
    IndexModel([("name", ASCENDING)], name="uniq_name", unique=True),
    IndexModel([("parent_manager_id", ASCENDING)], name="parent_manager_id"),
]


async def ensure_indexes(db: AsyncDatabase[dict[str, Any]]) -> None:
    """Create every declared index if missing. Safe to call on every
    process startup — see module docstring.
    """
    await db[SERVERS_COLLECTION].create_indexes(SERVER_INDEXES)
    await db[SITES_COLLECTION].create_indexes(SITE_INDEXES)
    await db[MANAGERS_COLLECTION].create_indexes(MANAGER_INDEXES)
