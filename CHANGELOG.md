# Changelog

What changed in each release, and what is new — in terms an operator or a
reviewer can act on, not a list of commit subjects.

**How releases happen here.** There is no manual release step. Every push
to `main` that passes CI publishes both images to GHCR and tags the
commit, with the version derived from Conventional Commits since the last
tag: `feat!:` or a `BREAKING CHANGE:` footer bumps major, `feat:` bumps
minor, anything else bumps patch
(`docs/adr/0010-image-publishing-and-versioning.md`).

That makes the version number automatic but says nothing about *why* a
release matters. This file is that missing half, so:

- **Keep `## Unreleased` current as you work.** Anything that changes
  behaviour, configuration, or the operational contract gets a line when
  it is committed — not reconstructed later from `git log`.
- **Write for the person deploying it**, not the person who wrote it. Name
  the environment variable, the endpoint, the exit code.
- **Breaking changes lead**, and say what an operator has to do.
- Pure internals — a refactor, a test, a doc typo — do not need a line.
  If nobody outside the repo could notice it, leave it out.

Releases below `## Unreleased` were reconstructed from the tag history,
so they read as commit subjects. Entries from here on should read better
than that.

---

## Unreleased

> **This heading is wrong and the entries under it are not.** Releases here
> are unattended (ADR-0010 tags and publishes on every push to `main`), so
> the "cut a version, open a fresh `## Unreleased`" step in the rules above
> has no moment at which anyone performs it. The result is that entries pile
> up under `## Unreleased` *after* they have shipped: the `SiteCode` break
> below went out in **v7.0.0** on 2026-08-29, and the repository is on
> **v8.2.0**. Fixing this properly means either splitting this section
> across the tags that actually carried it, or having the release job
> rewrite the heading when it tags. Until one of those happens, read this
> section as "recent", not as "unreleased".

### Breaking

- **`SiteCode` is gone; the site list is configuration.** Set
  `INVENTORY_SITES="nyc:New York City,tlv:Tel Aviv,bat-yam:Bat Yam,five:Site Five"`
  (Helm: `config.sites`). Leaving it unset keeps the four sites that
  shipped before, so no action is required to stand still. Renaming or
  adding a site is now one variable and a pod restart — it reaches the
  API, the site cards, the inventory filter, both policy editors and the
  seeded classification rules with no rebuild. `Server.site_id` is a
  plain string, so servers stored under a site that is later renamed away
  still load. See `docs/adr/0018-sites-from-configuration.md`.
- **Intersight credentials are named for what they are.**
  `INVENTORY_INTERSIGHT_USERNAME`/`_PASSWORD` became
  `INVENTORY_INTERSIGHT_API_KEY_ID`/`_API_KEY_PEM`, and the Helm value
  `collectors.intersight.privateKey` became `.apiKeyPem`. Intersight has
  no password login at all; calling an API key a password made the wrong
  action the obvious first guess.
- **Sites were renamed** to `nyc`, `tlv`, `bat-yam` and `five`, and a
  Cisco server whose name carries no site token now falls back to its
  service profile's org DN.
- **The Intersight collector no longer verifies TLS certificates, ever.**
  `INVENTORY_INTERSIGHT_CA_BUNDLE` is gone, and there is no verify flag —
  `IntersightClient` now hardcodes `verify=False` unconditionally. This
  applies to every Intersight connection this codebase makes, including a
  real production SaaS or on-prem tenant, not only a lab appliance. An
  operator who was relying on `INVENTORY_INTERSIGHT_CA_BUNDLE` for a
  trusted internal CA loses certificate verification with no setting to
  restore it — reintroducing one is a small, self-contained change; see
  `docs/adr/0017-intersight-collector.md`'s 2026-08-31 update for the
  shape it had before this.

### New features

- **Cisco Intersight collector** (`--manager-type INTERSIGHT`). The first
  collector whose cost does not scale with the fleet: every sub-resource
  is listed once for the whole estate and joined in memory, roughly 120
  requests for 10,000 servers against the Redfish collector's ~25 *per
  BMC*. Deployed entirely from `values.yaml` with no pre-existing Secret
  and no mounted key file. Servers in `UCSM` mode are excluded by default
  because UCS Central already owns them; override with
  `INVENTORY_INTERSIGHT_MANAGEMENT_MODES`.
  **First run against a real on-prem tenant (2026-09-01)** confirmed
  auth, name resolution and the `TotalMemory` unit (MiB, as assumed) —
  see `docs/adr/0017-intersight-collector.md`'s "Validation" section
  before scheduling it; a full `--dry-run` ingest is still outstanding.
- **`tools/verify_intersight.py`** — a read-only pre-flight that proves
  the API key, reports what the tenant holds, and settles whether
  `TotalMemory` is MiB by summing a real server's DIMMs. Run it first.
- **The Intersight collector now reports `cpu_model`.** Read from
  `processor.Unit`, the same fleet-wide-listable cost class as the
  collector's other sub-resource joins — this was cut in the original
  build on a since-corrected assumption; see ADR-0017's Decision 5.
- **A fleet-wide "Across all sites" card** on the sites overview, and
  `standalone` now labelled properly in every vendor breakdown.
- **BMC addresses display as plain hosts** in the UI and the collector
  dry run — no `redfish://…/v1/Systems/1`, no `:623`. The full URI is
  still stored for the Metal3 round-trip.

### Fixed

- **The Intersight collector was silently missing every drive, and on
  some hardware every GPU and CPU model, because `storage.Controller`
  never joined to its server.** Confirmed live against a real tenant: 0
  of 37 storage controllers set the relationship the collector read
  (`ComputeBlade`/`ComputeRackUnit`) at all — every one set only
  `ComputeBoard`, a relationship the collector never followed.
  `graphics.Card` and `processor.Unit` carry the same relationship and
  got the same fix pre-emptively. No action needed — this ships fixed;
  the collector has never had a scheduled production run to have been
  under-reporting in.
- **`--dry-run` no longer prints fabric-interconnect fields on a vNIC
  attachment.** A vNIC structurally never carries a fabric relationship —
  every `[VNIC ...]` line used to print `fabric None … FI
  model/serial=—/—` regardless, which read as missing data. A
  standalone server (no cable to a Fabric Interconnect it doesn't have)
  now shows no FI-shaped line at all, as a direct consequence rather
  than a special case. Applies to every provider's dry-run output, not
  only Intersight's.
- **Intersight was missing from the UI's Source filter**, so a whole
  collector's servers could not be filtered for. `REDFISH_STANDALONE` was
  likewise missing from the manager-type picker in both editors, so no
  rule or policy could be scoped to it. A test now fails the build if the
  frontend's copies of `ManagerType` drift from the backend again.
- **Seeded classification rules are re-synced when their definition
  drifts**, keeping their id, match stats and `enabled` flag. Before this,
  a database seeded earlier kept stale site patterns forever.
- A failed sub-resource query could report an empty NIC list rather than
  "unread", which would have overwritten stored MAC addresses.
- The Intersight run budget did not cover the phase where the time is
  actually spent, and `bmc_address` preferred a less specific source than
  the one its MAC comes from.
- Ingest built four of a server's fields into a dict and `**`-unpacked it
  into the model, which suppressed argument checking for the entire call.
  No stored data was ever wrong because of it, but the carry-forward set
  — what a re-collection preserves rather than overwrites — was invisible
  at the call site. It is now spelled out, along with the two collected
  sub-resources (`network.interfaces`, `connectivity.attachments`) that
  are *not* carried forward and cannot be until the provider protocol can
  say "could not read" for them.

### Contributor-facing — **mypy is gone; ty is the type checker**

Nothing to deploy. This changes what the build checks, not what it ships:
the runtime images are unaffected, and `requirements.txt` / `pylock.toml`
are unchanged, since both are exported `--no-dev` and neither checker was
ever in them.

It does change what a contributor has to do, so:

- **The gate's third step is now `uv run ty check backend/app tools`.**
  Full local gate:
  `uv run ruff check . && uv run ruff format --check . && uv run ty check backend/app tools`.
  `uv sync --all-groups` installs everything; ty is a pinned dev
  dependency (`ty==0.0.76`), not a GitHub Action. Cold type-checking in
  CI went from ~15 s to 0.3 s.
- **Suppression comments are `# ty: ignore[rule-name]`.** ty honours a
  *bare* `# type: ignore` but not a coded one — it does not know mypy's
  rule codes — so an old `# type: ignore[return-value]` silently
  suppresses nothing.
- **Annotations are enforced by ruff**, via `ANN001`–`ANN206` (not
  `ANN401`). Same contract mypy's `disallow_untyped_defs` enforced, moved
  somewhere that outlives mypy: ty has no equivalent rule and cannot grow
  one. It covers `tests/` too, which mypy never checked.
- **ty is beta software on 0.0.x and is pinned exactly** for that reason,
  as is `types-regex` now. Expect diagnostics to move when either is
  bumped on the quarterly pass — a new error after a bump is the tool
  changing, not a regression in this codebase.

`docs/adr/0019-ty-replaces-mypy.md` has the measurements, why ty over
Pyrefly and Pyright, the six rules switched off and why, the three mypy
false positives this surfaced, and the rollback triggers.

### Documentation

- `docs/arc42.md` — a structured architecture overview with goals,
  constraints, deployment view, quality scenarios and an honest risk and
  technical-debt register.
- `docs/diagrams/runtime-architecture.html` — an interactive runtime
  diagram with repository evidence pinned to a commit.
- `docs/field-test-checklist.md` — exactly what to run against a real
  Intersight, and what to send back.
- Corrected a standing inaccuracy: there is no `AuthProvider`/RBAC
  scaffolding. Every endpoint is unauthenticated, writes included.

---

## v4.0.2 — 2026-08-23

### Fixed

- Merge NVIDIA DGX/HGX GPU-baseboard systems into their host instead of ingesting them separately


## v4.0.1 — 2026-08-23

### Fixed

- Print GPU detail in the collector dry-run, not just a count


## v4.0.0 — 2026-08-23

### New features

- **Breaking.** Map a missing Redfish Manufacturer to STANDALONE instead of failing the system

### Documentation

- Fill in example Redfish inventory/credentials file paths in .env.example


## v3.7.0 — 2026-08-23

### New features

- Collect GPU memory type, ECC status, error counts, temperature and power


## v3.6.2 — 2026-08-23

### Fixed

- Show every collected GPU field in the hardware tab, not just model


## v3.6.1 — 2026-08-23

### Fixed

- Fall back to summing the Memory collection when MemorySummary is absent
- Stop forcing a trailing slash onto the Redfish session-creation URI


## v3.6.0 — 2026-08-23

### New features

- Set uv index-strategy to unsafe-best-match for air-gapped mirror use


## v3.5.0 — 2026-08-23

### New features

- Collect standalone servers over Redfish


## v3.4.2 — 2026-08-23

### Documentation

- Settle the six open questions on the Redfish collector design


## v3.4.1 — 2026-08-23

### Fixed

- Let a collector report a field it could not read, and carry it forward
- Write the Manager projection on every collector run
- Index system_uuid only when it is a string, and migrate a respecified index

### Documentation

- Research, plan and draft ADR for a standalone Redfish collector


## v3.4.0 — 2026-08-18

### New features

- Distinguish physical vs vNIC fabric attachments and join Fabric Interconnect identity


## v3.3.0 — 2026-08-18

### New features

- Round dry-run storage to 1 decimal and print each server's profile DN

### Fixed

- Resolve the Cisco BMC management IP off the service profile's DN, not the compute unit's


## v3.2.1 — 2026-08-18

### Documentation

- Validate ADR-0014 against a live UCS Central


## v3.2.0 — 2026-08-18

### New features

- Render dry-run storage capacity as TiB above 1024 GiB

### Fixed

- Prefer the service profile's assigned management IP over mgmtIf.ext_ip for Cisco BMC addresses


## v3.1.0 — 2026-08-18

### New features

- Exit 3 when a collector run does not see the whole fleet


## v3.0.2 — 2026-08-18

### Documentation

- Add a runbook for testing the UCS collector against real hardware


## v3.0.1 — 2026-08-18

### Documentation

- Move Cisco collector evidence out of the code into a reference doc


## v3.0.0 — 2026-08-18

### New features

- **Breaking.** Collect Cisco through UCS Central, reading each domain live from its own UCS Manager
- Prefer vNIC MACs over physical-port MACs, print storage/CPU model in dry-run


## v2.4.1 — 2026-08-17

### Documentation

- Require the full local check, not just lint, before calling work done


## v2.4.0 — 2026-08-17

### New features

- Support Python 3.12 as a compatibility floor
- Map CPU model and storage detail for both Cisco collectors

### Fixed

- Apply ruff format to the CPU/storage collector changes
- Pin redis/ucsmsdk/ucscsdk/testcontainers to what the air-gapped mirror carries


## v2.3.0 — 2026-08-15

### New features

- Add a read-only probe that settles the UCS Central profile question


## v2.2.0 — 2026-08-15

### New features

- Collect every UCS domain in one run via a UCS Central collector


## v2.1.0 — 2026-08-15

### New features

- Restrict collection to servers whose name matches a pattern


## v2.0.9 — 2026-08-15

### Documentation

- Record the supply-chain work, and drop the redundant namespace step


## v2.0.8 — 2026-08-15

### Fixed

- Replace the tag action, which stops working when Node 20 is removed


## v2.0.7 — 2026-08-15

### Fixed

- Clear both frontend lint warnings


## v2.0.6 — 2026-08-15

### Fixed

- Drop python-multipart, an unused dependency with seven known CVEs


## v2.0.5 — 2026-08-15

### Housekeeping

- Remove the Dependabot config


## v2.0.4 — 2026-08-15

### Fixed

- Correct a wrong comment in the dependabot config


## v2.0.3 — 2026-08-15

### Fixed

- Pin every CI action to a commit SHA and update the stale ones


## v2.0.2 — 2026-08-15

### Documentation

- Document the vendor manager connection variables in the example env


## v2.0.1 — 2026-08-15

### Documentation

- Drop the duplicate OpenShift manifests and bring the docs up to date


## v2.0.0 — 2026-08-15

### New features

- **Breaking.** Configure vendor managers from env, one endpoint+login per type


## v1.1.0 — 2026-08-15

### New features

- Add --dry-run and --debug-xml to the collector runner


## v1.0.2 — 2026-08-15

### Fixed

- Name UCS servers after their service profile, not their DN


## v1.0.1 — 2026-08-14

### Fixed

- Correct UCS collector against a real UCS Manager (UCSPE 4.2)


## v1.0.0 — 2026-08-14

### New features

- **Breaking.** Closed site/vendor enums, name-derived sites, and a redesigned UI

### Fixed

- Repair E2E suite after the redesign and restore row link semantics
- Give every severity a distinct glyph and stop hiding maintenance

### Housekeeping

- Remove a stray screenshot scratch file committed by mistake


## v0.0.4 — 2026-08-14

### CI / build

- Cache uv, cancel superseded PR runs, trim workflow comments

### CI / build

- Cache uv, cancel superseded PR runs, trim workflow comments


## v0.0.3 — 2026-08-14

### CI / build

- Drop --with-deps from Playwright install, cutting ~10min from E2E

### CI / build

- Drop --with-deps from Playwright install, cutting ~10min from E2E


## v0.0.2 — 2026-08-14

### Fixed

- Correct UCS Manager collector queries that returned no usable data


## v0.0.1 — 2026-08-14

### Changed

- Fix CI: mathieudutour/github-tag-action has no rolling v6 tag
- Publish both images to GHCR on every push to main, versioned by Conventional Commits
- Refresh docs and add CLAUDE.md for session continuity
- Add first real vendor collector: Cisco UCS Manager
- Fix ruff format check on tools/loadtest.py and tools/verify_indexes.py
- Add Playwright E2E suite and give maintenance a real UI
- Add 10k/50k performance pass: index-coverage verification and request coalescing
- Add classification-rule and health-policy admin UIs
- Add slice 4: maintenance, audit trail, and server profile templates
- Add slices 2-3: classification engine and health policy engine
- Complete slice 1: inventory vertical slice
- WIP: slice 1 inventory vertical slice (domain, backend API, frontend UI)
- Add Phase 1 project skeleton: backend, frontend, dev stack, CI


