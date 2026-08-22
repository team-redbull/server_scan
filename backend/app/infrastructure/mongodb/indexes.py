"""Declarative MongoDB index definitions.

Indexes are declared here, as data, rather than issued as ad-hoc
`create_index` calls scattered through repository code — `ensure_indexes`
is called once per process at startup (see `app.main`'s lifespan) and is
safe to call every time: `create_indexes` is idempotent when an index
already exists with an identical spec.

When a declared spec *has* changed, `ensure_indexes` drops the stored
index and recreates it, logging `mongo.index_respecified`. It originally
let MongoDB's `IndexKeySpecsConflict` propagate, on the reasoning that
drift is a bug worth surfacing — but that reasoning had the direction
backwards. The conflict is raised by the deployment applying the *new,
correct* spec, so propagating it means a corrected index takes the API
and every collector down on startup instead of migrating. Drift between
this file and a database is not a mystery to investigate; this file is
the declaration, and the database is what follows it. See
docs/adr/0016-redfish-standalone-collector.md, where correcting
`uniq_system_uuid` surfaced this.

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

import structlog
from pymongo import ASCENDING, DESCENDING, IndexModel
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import OperationFailure

logger = structlog.get_logger(__name__)

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
        # `$type: "string"`, not `$exists: true`: MongoDB's `$exists` is
        # true for a field that is *present and null*, and `model_dump(
        # mode="json")` always emits `identity.system_uuid`. So `$exists`
        # admitted every UUID-less server into a unique index keyed on
        # null, letting exactly one of them exist fleet-wide. See
        # docs/adr/0016-redfish-standalone-collector.md.
        partialFilterExpression={"identity.system_uuid": {"$type": "string"}},
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
    IndexModel(
        [("maintenance.enabled", ASCENDING), ("name_normalized", ASCENDING), ("_id", ASCENDING)],
        name="maintenance_enabled_name_id",
    ),
    IndexModel([("updated_at", DESCENDING), ("_id", DESCENDING)], name="updated_at_id"),
    # Unfiltered sorts (no `FILTER_FIELDS` value supplied) still need a
    # supporting index per `SORT_FIELDS` entry, or they fall back to an
    # in-memory sort. `last_seen_at` originally shipped as a single-field
    # index with no `_id` tiebreak — unlike every other entry in this
    # block — which meant an unfiltered `sort=last_seen_at` request forced
    # a full COLLSCAN plus a blocking in-memory sort at 10k+ scale. Caught by
    # `tools/verify_indexes.py` running `.explain()` against a real 50k-
    # document collection — small enough test fixtures didn't expose it,
    # since MongoDB's planner is happy to pick a COLLSCAN over a
    # barely-selective index at low document counts anyway.
    IndexModel([("last_seen_at", ASCENDING), ("_id", ASCENDING)], name="last_seen_at_id"),
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


_INDEX_KEY_SPECS_CONFLICT = 86


async def _create_indexes(
    db: AsyncDatabase[dict[str, Any]], collection: str, indexes: list[IndexModel]
) -> None:
    """
    Create a collection's declared indexes, replacing any whose stored
    specification has since changed.

    MongoDB rejects `createIndexes` outright (`IndexKeySpecsConflict`)
    when an index of the same name exists with different options, so
    without this a changed specification does not merely fail to apply —
    it raises on every process startup and every collector run, taking
    the deployment down rather than migrating it. See
    docs/adr/0016-redfish-standalone-collector.md, where correcting
    `uniq_system_uuid`'s partial filter first surfaced this.

    Args:
        db (AsyncDatabase[dict[str, Any]]): The database to act on.
        collection (str): Collection whose indexes are being ensured.
        indexes (list[IndexModel]): The declared indexes.

    Raises:
        OperationFailure: For any failure other than a specification
            conflict, which is left to surface rather than be retried
            blindly.
    """
    try:
        await db[collection].create_indexes(indexes)
        return
    except OperationFailure as exc:
        if exc.code != _INDEX_KEY_SPECS_CONFLICT:
            raise

    # Rebuilt one at a time so a single changed specification cannot drop
    # indexes that were already correct.
    for index in indexes:
        name = index.document.get("name")
        try:
            await db[collection].create_indexes([index])
        except OperationFailure as exc:
            if exc.code != _INDEX_KEY_SPECS_CONFLICT or not name:
                raise
            logger.warning(
                "mongo.index_respecified",
                collection=collection,
                index=name,
                hint=(
                    "The stored index specification differs from the declared one; "
                    "dropping and recreating it. Expect a brief window with no index."
                ),
            )
            await db[collection].drop_index(name)
            await db[collection].create_indexes([index])


async def ensure_indexes(db: AsyncDatabase[dict[str, Any]]) -> None:
    """Create every declared index if missing. Safe to call on every
    process startup — see module docstring.
    """
    await _create_indexes(db, SERVERS_COLLECTION, SERVER_INDEXES)
    await _create_indexes(db, SITES_COLLECTION, SITE_INDEXES)
    await _create_indexes(db, MANAGERS_COLLECTION, MANAGER_INDEXES)
    await _create_indexes(db, HEALTH_POLICIES_COLLECTION, HEALTH_POLICY_INDEXES)
    await _create_indexes(db, CLASSIFICATION_RULES_COLLECTION, CLASSIFICATION_RULE_INDEXES)
    await _create_indexes(db, AUDIT_EVENTS_COLLECTION, AUDIT_EVENT_INDEXES)
