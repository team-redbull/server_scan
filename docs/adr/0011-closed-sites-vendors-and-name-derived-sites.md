# ADR-0011: Sites and vendors are closed sets, and a server's site comes from its name

> **Partly superseded by
> [ADR-0018](0018-sites-from-configuration.md) (2026-08-30).** The
> decision that the site set is *closed* stands, and so does deriving a
> server's site from its own name. What changed is where the set lives:
> `SiteCode` is no longer a Python enum but a `SiteCatalog` loaded from
> `INVENTORY_SITES`, so an estate names its own sites without a code
> change. Read that ADR before acting on this one's enum details.


## Status

Accepted

## Context

`Server.site_id` was a free-form string and `Vendor` carried `HPE` plus an
`UNKNOWN` fallback. Both turned out to describe a system nobody has.

The site filter in the UI was a free-text box: an operator had to type an
opaque generated id (`site_fake_ams1`) exactly right, and one wrong
character returned an empty table indistinguishable from "this site has
no servers". Nothing enumerated the valid values, so there was no way to
pick one instead of guessing.

`Vendor.UNKNOWN` existed as a fallback in `IngestService` for a vendor
string it could not parse. But every server reaches this platform through
a vendor-specific collector — the vendor is a property of *which
collector produced the record*, not something inferred from a payload —
so `UNKNOWN` could only ever mean "a provider emitted something it
shouldn't", quietly bucketed into a category that then polluted every
per-vendor count in the UI.

Meanwhile the real estate's hostnames already carry both facts this
platform needs: the site (`ocp4-prod-**one**-infra-01`) and the
installation-type convention the classification rules match on.

## Decision

**Sites are a closed enum** (`SiteCode`: one, two, three, four, five) and
**vendors are exactly dell, cisco, hp** — `HPE` renamed to match what
operators here call it, `UNKNOWN` removed entirely. An unparseable vendor
now raises and is counted in `IngestSummary.errors` rather than being
absorbed.

**A server's site is parsed from its own name**
(`app.domain.value_objects.site.parse_site_code`), not taken from the
collector's configuration. A manager configured with the wrong site would
otherwise mislabel every server it collects, with nothing downstream able
to notice; parsing the name makes the label self-correcting — rename the
host and the platform agrees on the next collection. `ProviderServer` no
longer carries a `site_id` at all, so no provider can assert one.

The parser matches whole `-`/`_`/`.`-delimited tokens, never substrings.
That is the entire reason it is not a simple `in` check: the site names
are short, common English words, and `ocp4-stone-01` contains "one" while
naming no site. A name holding two different site tokens yields `None`
rather than picking the leftmost — an ambiguous hostname is a naming bug
worth surfacing.

`None` is a real, surfaced state ("Unassigned"), never defaulted to a
site. Mislabelling a machine's location is worse than admitting the name
does not say.

**New `GET /api/v1/sites`** returns all five sites plus an `unassigned`
bucket with per-vendor, per-health and maintenance counts, from a single
`$group` rather than ~150 count queries, behind a 30-second cache. Every
site is present whether or not it has servers: "site four is empty" and
"site four does not exist" are different facts.

### UI

The site overview became the landing page. At ~10,000 servers a flat
list cannot answer "is anything wrong?" without sorting and scanning;
five cards can, and each links into a pre-filtered list.

The inventory table went from nine columns to three — name, model,
state. Site is redundant in every row now that it lives in the hostname,
and everything else is one click away on the detail page with room to
present it properly.

Health severity and the maintenance flag render in **one** State column.
An earlier iteration let maintenance *replace* the severity, on the
theory that a critical alert on a machine someone is already working on
trains people to ignore red. That reasoning is real but the fix was
wrong: it hid the severity behind a tooltip, so "critical, and someone is
on it" and "in maintenance, otherwise fine" rendered identically.
Maintenance is now its own chip beside the severity, in a hue outside the
severity set.

Every severity carries a **distinct glyph and its own word**, so colour is
a third, redundant signal. This is load-bearing: an earlier version gave
HEALTHY and INFO the same filled circle, which made them identical to a
reader who cannot separate green from blue — exactly the reader the glyph
exists for. A test asserts the glyphs stay mutually distinct, and one
shared `SEVERITY_GLYPH` map backs both the table and the site cards after
they were briefly found drawing critical and warning as opposite shapes.

## Consequences

- **Breaking.** Existing data must be re-seeded: site ids change from
  `site_fake_<code>` to bare codes, and `Server.identity` is now required
  because `Identity.vendor` has no default.
- The classification rules had to be rewritten as well — the seeded
  defaults (`^ocp-.*`, `^upi-.*`) matched none of this estate's real
  hostnames. See the anchored, mutually exclusive patterns in
  `app.infrastructure.mongodb.classification_rule_repository`, and note
  that their tests assert classification *behaviour* against real
  hostnames rather than rule shape: the old tests passed against patterns
  that matched nothing real.
- A UCS-sourced fleet only gets sites once its hostnames carry them. A
  server with no service profile reports an empty name, so the collector
  falls back to its DN (`sys/chassis-3/blade-1`), which has no site token
  — correct, and visibly distinct in the "Unassigned" bucket.
- The fake data generator now emits the real hostname shapes, so
  dev/CI fixtures classify and resolve sites the same way production
  does. A deliberate minority carry no site token at all, keeping the
  "Unassigned" and "Unclassified" states reachable in dev.
