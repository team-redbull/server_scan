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

- **Keep `## Unreleased

> **This heading is wrong and the entries under it are not.** Releases here
> are unattended (ADR-0010 tags and publishes on every push to `main`), so
> the "cut a version, open a fresh `## Unreleased`" step in the rules above
> has no moment at which anyone performs it. The result is that entries pile
> up under `## Unreleased` *after* they have shipped: the `SiteCode` break
> below went out in **v7.0.0** on 2026-08-29, and the repository is on
> **v10.1.0**. Fixing this properly means either splitting this section
> across the tags that actually carried it, or having the release job
> rewrite the heading when it tags. Until one of those happens, read this
> section as "recent", not as "unreleased".
>
> **The version the current range will produce: v10.2.0.** Everything since
> `v10.1.0` is `feat:`/`fix:`/`style:` with **no `feat!:` and no
> `BREAKING CHANGE:` footer**, so the next automatic tag is a minor bump.
> The `### Breaking` entries below are all older than that tag and are
> listed here only because this section never got cut — an operator
> upgrading from v10.1.0 has nothing breaking to act on.

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

- **HPE servers are collected, through OneView.** Set
  `INVENTORY_ONEVIEW_IP`/`_USERNAME`/`_PASSWORD` and run
  `--manager-type ONEVIEW` (Helm: `collectors.oneview.enabled: true`,
  which schedules a CronJob every 4 hours). **OneView is the only source
  for every HPE server whatever its iLO generation** — there is no BMC
  login, no Redfish pass and no per-generation branch, so an iLO 4
  machine is collected by the same code path as a Gen11 and simply
  reports less. Three bulk calls cover the whole appliance:
  `GET /rest/server-hardware` returns the complete object per member, and
  `expand=all` folds in each server's DIMMs, drives, GPUs and PCI
  devices. A read-only OneView account is enough; the session is deleted
  on the way out.
  - **`INVENTORY_ONEVIEW_COLLECT_PSUS`** (default `true`) — power
    supplies are the one thing OneView will not return in the bulk sweep,
    so they cost one request per server: the difference between a
    ~15-request run and a ~2500-request one.
    **`INVENTORY_ONEVIEW_PSU_CONCURRENCY`** (default 8) bounds the
    fan-out. Turning it off means every HPE server's `psus` reads as
    *unread* and the stored value is carried forward, never erased.
  - **`INVENTORY_ONEVIEW_API_VERSION`** pins the `X-Api-Version` OneView
    requires on every call. Leave it unset — the default — and the
    appliance is asked what it supports and clamped to the newest version
    this collector was written against (8000 / OneView 10.20). Set a
    number only to roll forward or back after an appliance upgrade moves
    a field.
  - **`INVENTORY_ONEVIEW_VERIFY_TLS`** (default off, like the Dell BMC
    setting, because an appliance in an air-gapped estate ships a
    self-signed certificate) and **`INVENTORY_ONEVIEW_PAGE_SIZE`**
    (default 256, the documented ceiling on `/rest/server-profiles`,
    where `count=-1` means *64* rather than "all").
  - **One appliance per deployment.** HPE caps a OneView appliance at
    2500 servers (1024 on a hypervisor other than ESXi). An estate that
    outgrows one needs a second endpoint, which is deliberately not built
    — the symptom is not a crash but an `oneview.collection_truncated`
    ERROR, or a fleet that quietly stops growing.
  - **`uv run python -m tools.verify_oneview`** — a read-only probe
    answering, against a real appliance, what this collector could not
    settle from documentation. Its headline check is whether
    `processorCount * processorCoreCount` equals the real core count
    summed from `/processors`; if that disagrees, every server's core
    count is wrong fleet-wide. It also reports whether paging gets past
    the 256-profile ceiling, whether an iLO-4 server reports any hardware
    at all, what `mpModel` really contains, which management-processor
    address is reachable, whether `expand=all` includes power supplies,
    and whether HPE's GPU product names match the GPU catalog.
    **Nothing here has ever run against live HPE hardware** — there is no
    OneView equivalent of Cisco's UCS Platform Emulator — so run this
    before scheduling the CronJob. See
    `docs/adr/0022-oneview-only-hpe-collector.md`,
    `docs/hpe-collectors.md`, and `docs/field-test-checklist.md` part 2.

- **Dell servers are collected, through OpenManage plus each iDRAC.** Set
  `INVENTORY_OME_IP`/`_USERNAME`/`_PASSWORD` **and**
  `INVENTORY_OME_BMC_USERNAME`/`_PASSWORD` and run
  `--manager-type OPENMANAGE` (Helm: `collectors.ome.enabled: true`, a
  CronJob every 6 hours). This is the one collector that reads from two
  places on purpose, and the one that needs two logins: two bulk calls to
  the OME appliance say which servers exist and what the operator named
  them, and each server's own iDRAC is then read over Redfish for the
  hardware, because OME reports guessed-at values where the BMC reports
  measured ones. A run without both logins is refused up front, naming
  the variables, rather than failing per-BMC as "bad credentials".
  **Its cost is per server (~25 requests each), not per appliance**, and
  the collector pod needs egress to the whole BMC network.
  `INVENTORY_OME_BMC_PORT` (443) and `INVENTORY_OME_BMC_VERIFY_TLS`
  (off, because iDRACs ship a factory self-signed certificate) tune the
  BMC half; everything else — timeouts, budgets, fleet concurrency, the
  auth-failure guard — is the shared `INVENTORY_REDFISH_*` set. See
  `docs/adr/0020-dell-identity-from-ome-hardware-from-redfish.md`.

- **A Dell server now reports one NIC per physical port, each with a
  location.** A partitioned Dell NIC reports every NPAR function as its
  own interface with its own MAC, so a 4-port card with 4 partitions per
  port arrived as 16 NICs — the server's logical plumbing, not what is
  cabled. Only the first partition of each port is kept now, so that card
  reads as four. `ProviderNic` and `NetworkInterface` gained a
  **`location`** field, filled from the iDRAC FQDD, and each surviving
  Dell NIC is renamed to it — four interfaces all called "System Ethernet
  Interface" identified none of them. **`nic_macs` is deliberately left
  whole**: it is the identity correlation key, and a server already
  ingested carrying all sixteen MACs has to keep matching on any of them.

- **Every collector now reports power supplies.** `ProviderServer.psus`
  was added in an earlier release and nothing populated it, so the health
  engine's `power.psu_count` and `power.failed_psu_count` metrics had
  nothing to read — a server with a dead PSU reported HEALTHY on power
  exactly like one with two good ones. All five collectors fill it now:
  Intersight and UCS Manager/Central for rack units (a blade's supplies
  belong to its shared chassis, not to the blade, so a blade reports
  none), the shared Redfish mapping for **Dell and every standalone BMC**,
  and OneView for HPE with the most precise data of the four. **An estate
  with a long-dead PSU will see health states change on the first sweep
  after upgrading — that is the feature working, not a regression.**
  Two rules if you are writing a policy against these metrics: a PSU's
  `health` is `UP`/`DOWN`/`DISABLED`/`UNKNOWN`, so the comparison is to
  `"DOWN"` and not `"FAILED"`; and an *absent* supply is dropped rather
  than counted failed, so a four-bay chassis with two supplies fitted is
  not a server with two failed PSUs.

- **A server now says which fields its last collection could not read.**
  `GET /api/v1/servers/{id}` carries a new `unread_fields` list of dotted
  paths into that same response — `hardware.storage.drives`,
  `hardware.gpus`, `identity.nic_macs` and so on. A collector that cannot
  read a subresource has always had its previous value carried forward
  (or the model's zero, for a server nobody has read yet), which kept the
  data correct but left the API unable to say the value was not confirmed.
  An HPE Gen9 whose iLO 4 refuses OneView's subresource calls no longer
  reports "0 drives, 0 bytes" as fact: the Hardware tab shows **"Not
  reported"** where the stored value is the zero, and dims a
  carried-forward value with a tooltip rather than hiding it. The list is
  recomputed from scratch on every ingest, so a field stops being flagged
  the moment a run reads it. No configuration, and no existing field
  changed type — `storage.total_bytes` is still an `int`.

- **GPU VRAM is now filled in out of the box, on every vendor, with no
  configuration.** The platform ships a built-in table of 30 NVIDIA and
  AMD datacenter GPUs (V100 through H200 and B200, T4, the A- and
  L-series, AMD Instinct MI100 through MI355X), every capacity taken from
  a vendor datasheet or a Cisco UCS spec sheet. No management plane this
  platform collects from reports a GPU's memory size at all — confirmed
  against both Cisco SDKs, Cisco's own metrics API, Redfish and OneView —
  so this table is the only source there has ever been. Cards are matched
  by Cisco PID *and* by the model string Dell's iDRAC and HPE's iLO
  report (`NVIDIA A100-PCIE-40GB`, `NVIDIA H100 80GB HBM3`), so a
  non-Cisco GPU gets a VRAM figure for the first time. Matching ignores
  case, whitespace, separators, a leading vendor word, a leading
  `HPE`/`HP`, a trailing marketing noun (`Accelerator`, `Kit`, …) and a
  trailing bus word (`PCIe`, `SXM4`) — and is exact otherwise, so `A10`
  and `A100` never cross-match. A trailing capacity is accepted only when
  it agrees with the row's own VRAM; one that disagrees matches nothing
  rather than reporting a wrong number. A card that shipped in two
  capacities (`A100`, `V100`, `H100`, `P100`) has no bare-name row and
  reports no VRAM rather than guessing.
  **`INVENTORY_GPU_MODELS` (Helm: `config.gpuModels`) changed meaning: it
  now overrides the built-in table instead of being the only source.** No
  action needed — an existing value keeps working and still wins for the
  identifiers it names, and every built-in row it does not name now
  applies too. Leaving it empty no longer means "enrich nothing". A
  vendor-reported memory value still always beats the catalog. See
  `docs/adr/0021-built-in-gpu-catalog-with-model-matching.md`.

- **The sites overview leads with three fleet-wide cards** — everything,
  UPI, and Hosted cluster — above the per-site cards. Each links straight
  into the pre-filtered server list (`/servers?installation_type=UPI`).
  `GET /api/v1/sites` grew a `by_installation_type` object on every site
  record, keyed by `HOSTED_CLUSTER`/`UPI`/`UNCLASSIFIED` and reporting
  the same total/health/vendor/maintenance counts a site does; no
  existing field changed. `standalone` is now labelled properly in every
  vendor breakdown. Nothing to configure.

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
  It also reports `cpu_model` (read from `processor.Unit`, the same
  fleet-wide-listable cost class as its other joins) and PSU identity and
  health for rack/standalone servers.
- **`tools/verify_intersight.py`** — a read-only pre-flight that proves
  the API key, reports what the tenant holds, and settles whether
  `TotalMemory` is MiB by summing a real server's DIMMs. Run it first;
  `docs/field-test-checklist.md` part 1 is the operator-facing version.
- **UCS Manager/Central report GPU identity and real temperature
  telemetry** — from `graphicsCard`, not the also-existing
  `coprocessorCard`, which Cisco's own UI documentation never ties to
  GPU hardware. Works for blades and rack units alike, unlike PSUs.
  `temperature_celsius` is genuine sensor data (unlike every other
  Cisco collector in this platform, which reports GPU telemetry as
  `None` — a real capability ceiling of their object models, not this
  one); its unit is assumed Celsius by convention, unverified against
  live hardware. See `docs/cisco-collectors.md`, "GPUs (coprocessor
  cards vs. graphics cards)". One more query per domain (13 now, was
  9 before this and the PSU addition above).
- **BMC addresses display as plain hosts** in the UI and the collector
  dry run — no `redfish://…/v1/Systems/1`, no `:623`. The full URI is
  still stored for the Metal3 round-trip.

### Fixed

- **`INVENTORY_GPU_MODELS` was silently ignored — the `Settings` field
  was named `gpu_model_catalog`, which pydantic-settings reads as
  `INVENTORY_GPU_MODEL_CATALOG`, a name nothing else in this repo (this
  file's own earlier entry included) ever documented or set.** Setting
  `INVENTORY_GPU_MODELS`, exactly as `.env.example` and the Helm chart
  say to, silently enriched nothing — no error, no warning, because an
  unrecognized env var is ignored by design (same as an unset one). The
  field is now named `gpu_models`, matching `INVENTORY_GPU_MODELS`
  letter for letter, the same convention `sites`/`INVENTORY_SITES`
  already used correctly. Any deployment that already set
  `INVENTORY_GPU_MODELS` needs no change — it now actually takes effect
  where before it silently didn't.
- **A UCS Central run killed at its deadline (or OOMKilled) used to lose
  every domain's data, not just the ones still in progress.**
  `list_servers()` gathered every concurrently-collected domain before
  yielding any of them, so nothing reached Mongo until the whole batch
  finished. Now streams each domain's servers the moment that domain
  completes (`asyncio.as_completed`, matching the Redfish collector's
  existing pattern) — a domain that finished before a kill keeps its
  data regardless of what else was still running. The per-domain coverage
  log (`ucs_central.domain_summary`) streams with it, rather than being
  the one thing still batched to the end of the run. No action needed;
  this ships fixed.
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
- **Intersight and `REDFISH_STANDALONE` were missing from the UI**, so a
  whole collector's servers could not be filtered for and no rule or
  policy could be scoped to a standalone BMC. A test now fails the build
  if the frontend's copies of `ManagerType` drift from the backend again.
  (The inventory page's separate **Source** filter list is not covered by
  that guard and still offers only three values — `OPENMANAGE` and
  `ONEVIEW` servers cannot be filtered for by source yet.)
- **Seeded classification rules are re-synced when their definition
  drifts**, keeping their id, match stats and `enabled` flag. Before this,
  a database seeded earlier kept stale site patterns forever.
- A failed sub-resource query could report an empty NIC list rather than
  "unread", which would have overwritten stored MAC addresses.
- The Intersight run budget did not cover the phase where the time is
  actually spent, and `bmc_address` preferred a less specific source than
  the one its MAC comes from.
- **`--dry-run` output corrections.** A vNIC attachment no longer prints
  fabric-interconnect fields: a vNIC structurally never carries a fabric
  relationship, so every `[VNIC ...]` line used to print `fabric None …
  FI model/serial=—/—` regardless, which read as missing data. A
  standalone server now shows no FI-shaped line at all. Redfish PSUs now
  print the raw `Health`/`State` pair the mapping collected, rather than
  only the reduced verdict. Both apply to every provider's dry run.
- **The carry-forward set is now spelled out at the call site.** Ingest
  used to build four of a server's fields into a dict and `**`-unpack it
  into the model, which suppressed argument checking for the whole call.
  No stored data was ever wrong because of it, but it is worth knowing
  which fields a re-collection preserves — and that
  `network.interfaces` and `connectivity.attachments` are **not**
  carried forward and cannot be until the provider protocol can say
  "could not read" for them.

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
  Intersight (part 1) and a real OneView (part 2), and what to send back.
- `docs/hpe-collectors.md` and `docs/notes/oneview-api.md` — the verified
  HPE implementation facts and the primary-source research behind them,
  in the shape `docs/cisco-collectors.md` already had for Cisco.
- `docs/dell-collectors.md` — the same for Dell, including the OME
  heuristics ADR-0020 replaced with measured Redfish values.
- Corrected a standing inaccuracy: there is no `AuthProvider`/RBAC
  scaffolding. Every endpoint is unauthenticated, writes included.
- **`deploy/air-gapped-images.txt`** — every container image an
  air-gapped deployment needs, in one place: the two published
  application images, their UBI build-time base images, and the
  local-dev-only MongoDB/Redis images, each labelled required-to-deploy
  vs. required-only-to-build. `docs/air-gap.md`'s own image table was
  also stale (`ubi-minimal:9.4`, the base image moved to `9.8` in a
  prior release) — it now points here instead of duplicating pins.

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


