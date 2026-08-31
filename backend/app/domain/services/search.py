"""Search/filter/sort whitelists for `GET /api/v1/servers`.

Every filter key, sort field, and the search string itself is validated
against an explicit whitelist before it ever reaches a Mongo query —
nothing here accepts an arbitrary field path or builds a query fragment
from a caller-supplied key. `app.infrastructure.mongodb.server_repository`
is the only consumer of `build_filter_query`/`resolve_sort_field`/
`build_search_query`; both `filters` and `sort` on
`ServerRepository.list_page` accept the raw (whitelist-key) form the API
layer receives — the repository is where the translation to real Mongo
field paths happens, via this module, so query-shape logic lives in one
place regardless of whether it's reached through a filter, a sort, or a
cursor.

Search itself is never raw regex against arbitrary user input: it's an
escaped, anchored-prefix match against the multikey-indexed
`search_tokens` field built by `app.domain.services.search_tokens`. An
anchored (`^`), escaped (`re.escape`) prefix is the one shape of user
input that's both safe (no ReDoS, no injection) and index-friendly (a
prefix regex against a sorted multikey index reduces to a bounded range
scan, not a collection scan).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime

from app.domain.models.server import Server
from app.errors import (
    SearchQueryTooLongError,
    SearchQueryTooShortError,
    UnknownFilterError,
    UnknownSortFieldError,
)

# API filter query-param name -> real Mongo field path on the `servers`
# collection. Deliberately explicit (not derived from the `Server` model's
# field names) so a domain-model rename doesn't silently change or break
# the public query-param contract.
FILTER_FIELDS: dict[str, str] = {
    "site_id": "site_id",
    "vendor": "identity.vendor",
    "manager_id": "manager_id",
    "installation_type": "classification.installation_type",
    "health_overall": "health.overall",
    "maintenance": "maintenance.enabled",
    # Which collector produced a server, and so how it is reached.
    # `?source_provider=REDFISH_STANDALONE` is what answers "these have
    # no manager — do not look for them in OpenManage or UCS".
    "source_provider": "source_provider",
}

# API sort query-param name -> real Mongo field path. Every value here
# must be a field that is always present with a safe default on every
# document (see `Server`'s docstring) and covered by a compound index in
# `app.infrastructure.mongodb.indexes` alongside `_id` — that's what keeps
# keyset pagination gap/duplicate free.
SORT_FIELDS: dict[str, str] = {
    "name": "name_normalized",
    "serial": "identity.serial_normalized",
    "model": "model_normalized",
    "updated_at": "updated_at",
    "last_seen_at": "last_seen_at",
}

MIN_SEARCH_QUERY_LENGTH = 2
MAX_SEARCH_QUERY_LENGTH = 64

# Extracts the value a keyset cursor is built from for each sort field,
# given a decoded `Server`. Kept alongside `SORT_FIELDS` because the two
# must stay in lockstep: `SORT_FIELDS[k]` is the Mongo path the query sorts
# on, `SORT_ACCESSORS[k]` is how the repository reads that same value back
# off the last item on a page to build the next cursor.
#
# `last_seen_at` falls back to the Unix epoch when unset: the domain model
# allows `None` there (a server that has never completed ingestion), but a
# cursor position must always be a concrete, comparable value. In practice
# every server upserted by `app.application.services.ingest` has
# `last_seen_at` set, so this fallback is a defensive edge case, not the
# common path.
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

SORT_ACCESSORS: dict[str, Callable[[Server], str | datetime]] = {
    "name": lambda s: s.name_normalized,
    "serial": lambda s: s.identity.serial_normalized,
    "model": lambda s: s.model_normalized,
    "updated_at": lambda s: s.updated_at,
    "last_seen_at": lambda s: s.last_seen_at or _EPOCH,
}


def build_filter_query(filters: dict[str, object]) -> dict[str, object]:
    """Translate whitelisted filter query-param names to real Mongo field
    paths. Raises `UnknownFilterError` for any key outside `FILTER_FIELDS`.
    """
    query: dict[str, object] = {}
    for key, value in filters.items():
        if key not in FILTER_FIELDS:
            raise UnknownFilterError(
                f"Unknown filter: {key!r}",
                details={"filter": key, "allowed": sorted(FILTER_FIELDS)},
            )
        query[FILTER_FIELDS[key]] = value
    return query


def resolve_sort_field(sort: str) -> str:
    """Translate a whitelisted sort query-param name to its real Mongo
    field path. Raises `UnknownSortFieldError` for anything else.
    """
    if sort not in SORT_FIELDS:
        raise UnknownSortFieldError(
            f"Unknown sort field: {sort!r}",
            details={"sort": sort, "allowed": sorted(SORT_FIELDS)},
        )
    return SORT_FIELDS[sort]


def build_search_query(raw_query: str) -> dict[str, object]:
    """Validate a raw search string's length and build the safe Mongo
    filter fragment for it.

    NEVER build a Mongo `$regex` from unescaped user input — `re.escape`
    is mandatory and this is the single place in the codebase that does
    it for server search, so there is exactly one thing to audit.
    """
    if len(raw_query) < MIN_SEARCH_QUERY_LENGTH:
        raise SearchQueryTooShortError(
            f"Search query must be at least {MIN_SEARCH_QUERY_LENGTH} characters.",
            details={"min_length": MIN_SEARCH_QUERY_LENGTH},
        )
    if len(raw_query) > MAX_SEARCH_QUERY_LENGTH:
        raise SearchQueryTooLongError(
            f"Search query must be at most {MAX_SEARCH_QUERY_LENGTH} characters.",
            details={"max_length": MAX_SEARCH_QUERY_LENGTH},
        )
    lowered = raw_query.lower()
    return {"search_tokens": {"$regex": "^" + re.escape(lowered)}}
