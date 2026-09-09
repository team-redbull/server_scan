# ADR-0026: nullable sort fields, and the index migration that was left manual

Date: 2026-09-10
Status: Accepted

Extends `docs/adr/0007-scale-verification-and-request-coalescing.md`, which
established keyset pagination, and `docs/adr/0024-openshift-cluster-
membership.md`, which added the two nullable fields this is about.

## Context 1: sorting the inventory by cluster or MCE

`Server.openshift.cluster_name` and `.mce_name` are `str | None` by
design — a server no cluster holds has no cluster name, and ADR-0024
records why that is `None` rather than `""`. Sorting the inventory on
either therefore means sorting on a nullable field, which every prior
sort field avoided: `name_normalized`, `model_normalized` and
`identity.serial_normalized` are always-present strings precisely so this
question never came up.

**MongoDB's range operators are type-bracketed.** Measured on 6.x:

| query | matches | why it matters |
|---|---|---|
| `{f: {$gt: null}}` | **nothing** | not "everything after null" |
| `{f: {$lt: "abc"}}` | strings only | **skips every null** |
| `{f: {$ne: null}}` | all strings | the usable form |

Sort order is separate and does span types: ascending puts every null
first, then strings; descending reverses it.

So the two disagree. `_cursor_position_clause` built
`{$or: [{f: {$gt: v}}, {f: v, _id: {$gt: id}}]}`, which is correct only
while `f` is never null. On a nullable field it strands rows silently —
descending, `$lt` excludes every null, so all of them vanish from the
walk with no error anywhere. Keyset pagination fails quietly by
construction: the caller gets a shorter list, not an exception.

## Decision 1

`CursorPosition.sort_value` becomes `str | datetime | None`, carrying its
own `_TYPE_NULL` tag rather than encoding `None` as `""` — null and the
empty string sort to different places, so conflating them reintroduces
the bug in a harder-to-see form.

`_cursor_position_clause` gains two null-aware branches:

| direction | position | clause |
|---|---|---|
| asc | null | `{f: {$ne: null}}` OR tie on `_id` |
| asc | string | `{f: {$gt: v}}` OR tie — nulls are already behind |
| desc | string | `{f: {$lt: v}}` OR **`{f: null}`** OR tie |
| desc | null | tie on `_id` alone — nothing sorts after null |

The `{f: null}` leg descending is the whole fix: type bracketing leaves
nulls out of `$lt`, and descending they are exactly what remains.

Verified over 600 seeded servers (181 null clusters, 385 null MCEs), page
size 23, both directions: every walk returned all 600 exactly once in the
correct order. An integration test pins it.

`cluster_name` and `mce_name` join `SORT_FIELDS`, backed by the
`openshift_cluster_name_id` and `openshift_mce_name_id` compound indexes
that already existed.

## Context 2: `uniq_system_uuid` kept rejecting servers

The same day, a collector run against a real deployment reported
`DuplicateKeyError ... index: uniq_system_uuid` for servers across four
vendors. That index was made non-unique and renamed to `system_uuid` on
2026-09-09, and `indexes.py` carried a comment saying so — while also
noting that `_create_indexes` reconciles on *name*, so the rename would
leave the old unique index in place on any already-deployed database, and
that an operator would have to drop it by hand.

Nobody did. The declaration said "not unique" and the database went on
enforcing uniqueness, exactly as the comment predicted, for as long as it
took someone to notice failing collector runs.

## Decision 2

`RETIRED_INDEXES` lists index names this file no longer declares, and
`ensure_indexes` drops them before creating the current set, logging
`mongo.index_retired`. An absent index is the normal case and is not an
error.

Renaming or removing an index now means adding its old name to that list,
and the migration happens on the next process start with no operator step.

## Consequences

- Any nullable field can now be a sort key. The cursor change is general;
  nothing about it is specific to these two fields.
- A retired index is dropped by whichever process starts first — the API
  or a collector — so there is a brief window on upgrade where neither
  index exists. Both of these are non-unique lookups, so the cost is a
  slower query, not a failure.
- `RETIRED_INDEXES` grows forever and is never safe to prune blindly: an
  entry can only be removed once no deployment could still be carrying
  that index.
- **The general rule, this session's third instance of it:** a change that
  is correct on a fresh database is not automatically correct on an
  existing one. Narrowing a persisted enum (ADR-0024's correction) and
  renaming an index both shipped green because every test ran against a
  database the new code had just created.
