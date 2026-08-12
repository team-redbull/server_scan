# ADR-0004: Escaped anchored-prefix token search, not raw regex or `$text`

## Status

Accepted

## Context

Operators need to search the inventory by name fragment, serial, MAC, or
hostname prefix. The spec explicitly warns against unrestricted user regex
reaching MongoDB. Three real options exist: MongoDB's `$text` index,
unrestricted `$regex`, or a normalized-token approach.

## Decision

At ingest time, `app.domain.services.search_tokens.build_search_tokens`
derives a `search_tokens` array per server: the lowercased full value of
name/serial/model/vendor/tags/site/manager plus each `-`/`_`/`.`-delimited
part, and — since MAC-keyed lookups are a real operator workflow (see the
existing PXE boot-script convention of naming files by bare-hex MAC) —
both the colon form and bare-hex form of every MAC address.

A user search query is lowercased, length-bounded (2–64 chars), and turned
into `{"search_tokens": {"$regex": "^" + re.escape(q)}}` — a left-anchored,
escaped, case-sensitive-against-already-lowercased-tokens match. Verified
in `tests/integration/test_server_repository.py` via `explain()` that this
plan is index-assisted (`IXSCAN` on the multikey `search_tokens` index),
not a collection scan.

## Why not the alternatives

- **`$text` index**: word-stemming and language rules actively hurt
  serial/MAC/model-number search (`"SN12"` should find `"SN123456"` — a
  prefix match — but `$text` doesn't do prefix matching), and a collection
  gets one `$text` index total, which is a real constraint against future
  needs.
- **Unrestricted `$regex`**: unanchored or unescaped user regex against
  MongoDB is a collection scan with no built-in timeout — a straightforward
  ReDoS vector, and exactly what the platform spec prohibits.
- **`re.escape` alone without anchoring**: still an unindexed collection
  scan; MongoDB only uses an index for a `$regex` when it's left-anchored.

## Consequences

- Search only matches from the start of a token, not mid-string. This is a
  documented, tested contract (`tests/unit/domain/services/
  test_search_tokens.py`), not an accidental limitation — `"lv-01"` will
  not find `"prep-tlv-01"`, `"prep"` will. If true substring search is
  needed later, the documented escape hatch is adding bounded suffix
  tokens for specific high-value short fields (e.g. serial), not switching
  strategies wholesale.
- `search_tokens` is capped (64 tokens, 64 chars each) so a pathological
  document (many tags, many NICs) can't blow up index size unboundedly.
- Every `search_tokens` write happens in the ingestion pipeline, in one
  place — there is no second code path that could reintroduce raw regex
  search by accident.
