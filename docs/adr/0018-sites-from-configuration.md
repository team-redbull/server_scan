# ADR-0018: Sites come from configuration, not from an enum

**Status:** Accepted, implemented 2026-08-30.

**Supersedes part of** `docs/adr/0011-closed-sites-vendors-and-name-derived-sites.md`
— specifically its decision that `SiteCode` is a Python enum. ADR-0011's
*other* two decisions are untouched and remain correct: a server's site
is still derived from its own name and never declared by a collector, and
the set of sites is still closed at runtime.

---

## Context

ADR-0011 made sites a `StrEnum` with four members, for a stated reason
worth repeating because it is a good one:

> the previous free-form `site_id` let a caller filter on a value no
> document could ever hold and get a silent empty result back, with
> nothing to distinguish "no such site" from "no servers there".

That reasoning is about the set being **closed**. It is not about the set
being **in the source code** — and conflating the two cost something
real. This platform is deployed air-gapped, into an estate whose sites
are `nyc`, `tlv`, `bat-yam` and `five`. The next estate's are not. A site
code is the token that appears inside that customer's hostnames; it is a
property of their naming convention, in exactly the way
`INVENTORY_COLLECTOR_NAME_PATTERN` already is.

With the enum in source, renaming a site meant editing Python, running
the test suite, rebuilding two container images, and shipping them
through an air-gapped mirror — for a change that alters no logic
whatsoever.

## Decision

**The set of sites is loaded from `INVENTORY_SITES` into a
`SiteCatalog`, and `SiteCode` is deleted.**

```
INVENTORY_SITES="nyc:New York City,tlv:Tel Aviv,bat-yam:Bat Yam,five:Site Five"
```

`code:Display Name`, comma-separated; the display half is optional and
falls back to a title-cased code. Empty means the shipped default, so dev
and CI configure nothing.

`SiteCatalog` is an immutable value object in the domain layer, built
from a plain string — it takes no dependency on `app.config`, so the
domain stays free of configuration and a test can pass a literal.

### The set is still closed, just closed at runtime

Everything ADR-0011 wanted from the enum survives:

- `GET /api/v1/sites` returns exactly the configured sites plus
  `unassigned`, so the UI still renders a card per site without
  null-checking, and a site with no servers still appears.
- The frontend has no site list of its own at all — it learns them from
  that endpoint, which is why **no frontend change was needed** for any
  of this.
- `parse_site_code` can only ever produce a configured code, so ingest
  cannot write a site that does not exist.
- The seeded classification rules interpolate `SiteCatalog.alternation()`,
  so reconfiguring sites rebuilds their patterns rather than leaving them
  silently behind.

### `Server.site_id` becomes `str`, deliberately

This is the one real trade and it is worth stating plainly. With an enum,
Pydantic rejected an unknown value; with a string, it does not.

That is the **correct** behaviour for a configuration-driven set. A
document written while `five` existed must still load after `five` has
been reconfigured away — otherwise renaming a site makes yesterday's
inventory unreadable, which is a far worse failure than an unrecognised
label. `SiteCatalog.name_for` title-cases an unknown code rather than
raising, so such a server renders as "Site Five" instead of vanishing or
500-ing.

Note also that this only ever applied to `Server.site_id`:
`HealthPolicyScope.site_id` and `ClassificationRule`'s scope were already
plain strings. The enum was never the fleet-wide guarantee it looked
like.

### Validation moves to startup, where it can be loud

A malformed `INVENTORY_SITES` raises `SiteConfigurationError` at startup
rather than at request time. Rejected: a code that is not
`[a-z0-9]+(-[a-z0-9]+)*`, a duplicate code, and a spec that parses to no
sites at all. Each of those would otherwise produce a site that appears
in the UI and that **no server can ever be assigned to**, because it
could never match a hostname token — a failure with no error anywhere and
an empty card as its only symptom.

Uppercase is normalised rather than rejected (`TLV` → `tlv`): hostname
matching is lowercase, so that is an operator being tidy, not a mistake.

## Consequences

**Good.** Renaming or adding a site is one environment variable and a pod
restart. It reaches the API, the site cards, the inventory filter, both
policy editors and the seeded classification rules at once, with no code
change, no image rebuild and no mirror round trip. Standing the platform
up for a different estate no longer starts with a patch.

**The ConfigMap is shared on purpose.** `INVENTORY_SITES` lives in the
`api-config` ConfigMap, which the API deployment *and* every collector
CronJob `envFrom`. They must agree: a collector derives each server's
site at ingest, so a collector with a stale list would write servers the
API cannot name. One key, one source.

**Reconfiguring does not retroactively re-site existing servers.**
`site_id` is written at ingest, so a renamed site takes effect for a
server on its next collection. The `Site` documents update on the next
seeder or collector run. This is the same eventual-consistency the
platform already has for every other derived field, but it is worth
knowing before someone renames a site and refreshes the UI expecting an
instant change.

**A seeded classification rule whose pattern drifts is re-synced on the
next API start** — that mechanism already existed, and it is what makes a
site change reach a database that was seeded before it.

**Cost accepted:** Pydantic no longer validates `Server.site_id`, and a
typo in `INVENTORY_SITES` that is *syntactically* valid (`tvl` for `tlv`)
produces a site nothing matches. Startup validation cannot catch that —
only looking at the resulting inventory can. `tools/run_collector.py
--dry-run` prints the resolved site per server for exactly this reason,
and `tools/verify_intersight.py` counts how many names resolved to a
site.
