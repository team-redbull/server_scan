# ADR-0025: search tokens are word-boundary suffixes, not bare parts

Date: 2026-09-10
Status: Accepted

Refines the search design `app.domain.services.search_tokens` has carried
since Phase 1. The query shape is unchanged — still an escaped, anchored
prefix against the multikey-indexed `search_tokens` field.

## Context

Server names here concatenate role, vendor, model, site, spec and serial:

```
ocp-cisco-m6-bat-yam-128c-1024gb-CIS0000010
ocp4-dok-five-compte-01
```

An operator searches by the part they know, which is almost never how the
name starts. Typing `cisco-m6` returned nothing.

The tokenizer indexed the whole value plus each `[^a-z0-9]+`-separated
part, so the tokens were `ocp`, `cisco`, `m6`, `bat`, `yam`, `128c`,
`1024gb`, `cis0000010` and the full name. The query is `^` + the escaped
input, and no token starts with `cisco-m6` — the full name starts with
`ocp-`. A fragment spanning two adjacent parts could not match anything.

## Decision

Index every **suffix of the value that starts at a word boundary**,
instead of the whole value plus its bare parts:

```
ocp-cisco-m6-bat-yam-128c-1024gb-cis0000010
    cisco-m6-bat-yam-128c-1024gb-cis0000010   <- ^cisco-m6 matches here
       m6-bat-yam-128c-1024gb-cis0000010
          bat-yam-128c-1024gb-cis0000010
              yam-128c-1024gb-cis0000010
                  128c-1024gb-cis0000010
                       1024gb-cis0000010
                              cis0000010
```

Separators come from the original value rather than being normalized, so
`10.1.2.3` yields `10.1.2.3` / `1.2.3` / `2.3` and a dotted search still
matches.

**Bare parts are no longer indexed separately.** A part is always a prefix
of the suffix that starts at it, so `^bat` already matches the token
`bat-yam-128c-...`. Token counts per server went slightly *down*.

### What this does and does not match

Whole word, or word-then-next-word, anywhere in the value:

| Query | `ocp4-dok-five-compte-01` |
|---|---|
| `dok` | matches |
| `dok-five` | matches |
| `five-compte` | matches |
| `compte-01` | matches |
| `ok-five` | **does not match** |

Matching from the middle of a word is the accepted limit, and it is a
choice rather than an oversight — see below.

## Why not just drop the `^` anchor

One character, and it would match `ok-five` too. Measured against 52,087
seeded servers through the real repository code path, best of 7 runs:

| | page (50 rows) | facets (per keystroke) |
|---|---|---|
| anchored prefix, bare parts (before) | 0.5–8.5ms | 0.7–24ms |
| **anchored prefix, suffix tokens (this ADR)** | **0.5–8.1ms** | **0.7–26ms** |
| unanchored substring | 6.3–8.7ms | **605–662ms** |

An anchored prefix against a sorted multikey index is a bounded range
scan. An unanchored regex is a full index scan — 332,728 keys and every
document, flat, regardless of how selective the term is. The inventory
page requests facets on every search, so that cost lands per keystroke.

Suffix tokens buy the capability at no measurable cost, because the query
is still a range scan. The trade is index size, bounded by the existing
64-token cap per document.

`with_count=true` is worse still under an unanchored regex — ~650ms
against ~1–3ms — but the frontend never sends it (`void with_count` in
`frontend/src/api/servers.ts`). Worth remembering if anything ever
turns it on.

Safety is unchanged either way: the input is `re.escape`d, so the pattern
is a literal and cannot backtrack. The rejection is purely about cost.

## Consequences

- `search_tokens` is written by `IngestService`, so existing documents
  keep their old tokens until the next collector run. Nothing breaks in
  between — old tokens still serve prefix queries — but a mid-name
  fragment will not find a server until it is re-ingested.
- Searching from the middle of a word stays unsupported. If that is ever
  wanted, the change is one character plus accepting the facet cost above,
  or an n-gram index, which would multiply index size by token length.
- `tests/unit/domain/services/test_search_tokens.py` asserts on whether an
  anchored query *would match*, not on token literals, so the token shape
  stays an implementation detail. One test pins the `ok-five` limit
  explicitly so it cannot regress silently into either direction.
