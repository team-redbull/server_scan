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
- The seeded classification rules interpolated `SiteCatalog.alternation()`
  at the time this was written, so reconfiguring sites rebuilt their
  patterns rather than leaving them silently behind. **No longer true as
  of 2026-09-08** — see "Cost accepted" and the dated update below.

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
restart. It reaches the API, the site cards, the inventory filter and
both policy editors at once, with no code change, no image rebuild and no
mirror round trip. Standing the platform up for a different estate no
longer starts with a patch. (It no longer also reaches the seeded
classification rules — see the dated update below.)

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
next API start** — that mechanism already exists (`app.application.
services.bootstrap.ensure_default_classification_rules`) and is what
makes a rule *definition* change reach a database that was seeded before
it. **Corrected 2026-09-08: no default classification rule interpolates
a site code any more** — the four `InstallationType` system defaults
became broad prefix/substring catch-alls with no site token at all
(`docs/architecture.md`'s "Ingestion wires both engines together"
section), so a site rename or addition no longer needs this mechanism to
reach classification. `SiteCatalog.alternation()`, which those rules used
to interpolate, is kept only for a future caller that needs "every token
this catalog recognises" as one pattern.

**Cost accepted:** Pydantic no longer validates `Server.site_id`, and a
typo in `INVENTORY_SITES` that is *syntactically* valid (`tvl` for `tlv`)
produces a site nothing matches. Startup validation cannot catch that —
only looking at the resulting inventory can. `tools/run_collector.py
--dry-run` prints the resolved site per server for exactly this reason,
and `tools/verify_intersight.py` counts how many names resolved to a
site.

## Update (2026-09-08): aliases — several tokens naming one site

Added at the operator's request: a site's code half in `INVENTORY_SITES`
may be `|`-separated aliases (`znif|prep:Znif`), so two naming
conventions for the same physical site — a renamed abbreviation, two
teams' different shorthand — combine onto one `Site`/one fleet card
instead of splitting across two. The first token is canonical: it is the
only one ever written as `Server.site_id`, used in a URL, or returned by
`codes`/`name_for` — the rest exist purely for `SiteCatalog.parse` to
recognise on the way in.

Every token, code or alias, still has to be globally unique across the
whole spec — reusing one anywhere else is rejected at startup with the
same "listed twice" error a duplicate plain code already got, for the
same reason: two sites claiming one token would make it ambiguous which
site a hostname carrying it names, exactly the ambiguity this whole
module exists to refuse to guess through. Two different aliases of the
*same* site appearing in one hostname is not that ambiguity — it resolves
to that one canonical code, the same as the bare code appearing twice
already did.

## Update (2026-09-09): substring matching, deliberately, false positives included

Reversed at the operator's explicit request, after being asked twice and
shown a concrete collision before implementing: a code or alias now also
matches as a **substring of a single token**, not only a token equal to
it outright. `ocp4-computezn-01` matches an alias `zn` glued in with no
separator of its own — the entire reason to add this was that requiring
a separator (`ocp4-compute-zn-01`) was not the estate's real naming
convention.

This is a direct reversal of this module's original design, which
existed specifically to reject `ocp4-tlvx-01` "containing" `tlv` while
naming no site — see the original "Decision" section's `_VALID_CODE`/
token-matching reasoning above, now half-superseded. That false-positive
risk is accepted consciously, not overlooked: the operator chose "every
code and alias, no distinction" over the narrower "aliases only" option
offered, after seeing the concrete collision below.

**A multi-token code (`bat-yam`) still never substring-matches** —
splitting a hostname on separators already removes every `-` from each
resulting token, so a code that carries its own `-` can never be a
substring of one. Substring matching only ever fires for single-token
codes/aliases, which is what short aliases like `zn`/`fn` are anyway.

**A real collision, found before this shipped, not after:** configuring
a site code or alias that is a substring of `infra` — the role token
this platform's own examples use everywhere
(`ocp4-prod-tlv-infra-01`) — makes every `-infra-`-named server
ambiguous rather than landing on the intended site: `fra` (a plausible
choice for Frankfurt) sits right inside `infra`, so a hostname carrying
both `lon` and `-infra-` now "matches" two different sites and gets
`None`, same as any other ambiguous name. `tests/unit/domain/
test_site_parsing.py::test_a_short_code_can_collide_with_infra_and_go_ambiguous`
pins this exact case. Avoiding common role words (`infra`, `compute`,
`worker`, `master`, `control-plane`, `prod`, `hypershift`) when picking a
code or alias is now the operator's job — startup validation cannot
catch this the way it catches an unusable character, because the
collision depends on every other code/alias configured and on hostnames
that do not exist yet.
