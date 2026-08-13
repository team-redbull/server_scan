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
CLASSIFICATION_RULES_COLLECTION = "classification_rules"
HEALTH_POLICIES_COLLECTION = "health_policies"
AUDIT_EVENTS_COLLECTION = "audit_events"

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
        # MongoDB partial-index filter expressions support only a small
        # operator subset ($eq, $exists, $gt/$gte/$lt/$lte, $type, and
        # $and of those) — no $ne. `$gt: ""` is the allowed-operator way
        # to express "non-empty string": every non-empty string sorts
        # lexicographically after "".
        partialFilterExpression={"identity.serial_normalized": {"$gt": ""}},
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

# `(enabled, policy_key, priority DESC, order ASC, _id ASC)` mirrors the
# exact family-resolution sort order `app.domain.services.health.evaluate.
# resolve_families`/`_family_sort_key` applies in memory after loading —
# loading the collection pre-sorted this way means the evaluator's own
# sort is over an already-ordered stream, not a hidden collection scan.
HEALTH_POLICY_INDEXES: list[IndexModel] = [
    IndexModel([("name", ASCENDING)], name="uniq_name", unique=True),
    IndexModel(
        [
            ("enabled", ASCENDING),
            ("policy_key", ASCENDING),
            ("priority", DESCENDING),
            ("order", ASCENDING),
            ("_id", ASCENDING),
        ],
        name="enabled_policy_key_priority_order_id",
    ),
    IndexModel([("policy_key", ASCENDING)], name="policy_key"),
    IndexModel([("category", ASCENDING)], name="category"),
    IndexModel([("scope.site_id", ASCENDING)], name="scope_site_id"),
]

# `(enabled, priority DESC, order ASC, _id ASC)` is literally the
# classification resolution order (see `app.domain.services.classification.
# _sort_key`, minus the in-memory specificity tiebreak that index can't
# express) — the standard "load all enabled rules" query filters on
# `enabled` and sorts on `(priority, order, _id)`, which is an IXSCAN over
# this single compound index end to end. The three single-field indexes
# below back admin filtering by scope (e.g. "show all rules scoped to this
# site/vendor/manager type"), not the resolution path itself.
CLASSIFICATION_RULE_INDEXES: list[IndexModel] = [
    IndexModel([("name", ASCENDING)], name="uniq_name", unique=True),
    IndexModel(
        [
            ("enabled", ASCENDING),
            ("priority", DESCENDING),
            ("order", ASCENDING),
            ("_id", ASCENDING),
        ],
        name="enabled_priority_order_id",
    ),
    IndexModel([("scope.site_id", ASCENDING)], name="scope_site_id"),
    IndexModel([("scope.vendor", ASCENDING)], name="scope_vendor"),
    IndexModel([("scope.manager_type", ASCENDING)], name="scope_manager_type"),
]

# Unlike the rule/policy/site/manager collections above, `audit_events` is
# unbounded and append-only — it grows for the lifetime of the deployment,
# never shrinks, and every read is a "most recent N, optionally filtered"
# query. All three indexes end in `_id DESC` to match the keyset
# pagination's fixed `(created_at DESC, _id DESC)` sort
# (`app.infrastructure.mongodb.audit_event_repository`), so every one of
# the three real read patterns — global feed, one server's history,
# one actor's history — is an IXSCAN, never an in-memory sort.
AUDIT_EVENT_INDEXES: list[IndexModel] = [
    IndexModel([("created_at", DESCENDING), ("_id", DESCENDING)], name="created_at_id"),
    IndexModel(
        [("server_id", ASCENDING), ("created_at", DESCENDING), ("_id", DESCENDING)],
        name="server_id_created_at_id",
    ),
    IndexModel(
        [("event_type", ASCENDING), ("created_at", DESCENDING), ("_id", DESCENDING)],
        name="event_type_created_at_id",
    ),
    IndexModel(
        [("actor.id", ASCENDING), ("created_at", DESCENDING), ("_id", DESCENDING)],
        name="actor_id_created_at_id",
    ),
]


async def ensure_indexes(db: AsyncDatabase[dict[str, Any]]) -> None:
    """Create every declared index if missing. Safe to call on every
    process startup — see module docstring.
    """
    await db[SERVERS_COLLECTION].create_indexes(SERVER_INDEXES)
    await db[SITES_COLLECTION].create_indexes(SITE_INDEXES)
    await db[MANAGERS_COLLECTION].create_indexes(MANAGER_INDEXES)
    await db[HEALTH_POLICIES_COLLECTION].create_indexes(HEALTH_POLICY_INDEXES)
    await db[CLASSIFICATION_RULES_COLLECTION].create_indexes(CLASSIFICATION_RULE_INDEXES)
    await db[AUDIT_EVENTS_COLLECTION].create_indexes(AUDIT_EVENT_INDEXES)
