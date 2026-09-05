# CLAUDE.md

This file orients a Claude Code session picking up this repository —
whether that's a fresh session or one resuming after a break. Read this
before making changes. `README.md` is the human-facing quickstart;
`docs/arc42.md` is the structured architecture overview (goals,
constraints, context, deployment, quality scenarios, and the risk and
technical-debt register); `docs/architecture.md` and `docs/adr/*` are the
technical deep-dives both of those point into rather than duplicate.

## What this is

A production-grade, air-gapped bare-metal server inventory platform:
MongoDB source of truth, FastAPI backend, React admin UI, Redis
cache-aside, a regex classification engine, and a declarative health-
policy engine. Target scale is **~10,000 physical servers with headroom
to 50,000+** — this is a real, primary requirement, verified at scale
(`docs/adr/0007-scale-verification-and-request-coalescing.md`), not a
stretch goal to hand-wave about.

The original 75-section spec that kicked this project off was given as
chat text early in the first session and was never saved as a repo file
— it's summarized in `docs/architecture.md`'s intent and the ADRs where
it mattered to a decision. **Treat it as background context for the big
picture, never as a literal spec to follow over actual current best
practice** — this was an explicit, repeated instruction from the user.

## Standing project conventions — follow these without being re-told

These came from explicit user instructions given across the sessions
that built this repo. They are not optional defaults; violating them
is a real mistake, not a style preference.

1. **Every non-trivial technical choice must be independently researched
   and justified on current merit** — never "the spec says so," never
   "a prior project did this." If you cite a reason in a code comment or
   ADR, it must be real, current-best-practice reasoning (RFC numbers,
   vendor docs, confirmed library behavior), not precedent. The user
   explicitly does not want technology reused just because it appeared
   in their own past projects (e.g. `dhcp_scope_manager`) — research
   fresh for this project's actual constraints (air-gapped, ~10k scale)
   every time.
2. **Git: commit and push after each completed unit of work**, with
   clear, understandable commit messages. **The user must be the only
   visible contributor** — every commit is authored as
   `TomerKarniol <tomer.karniol@gmail.com>` (check `git log --format="%an <%ae>" -1`
   after committing to confirm), and **never** include a
   `Co-Authored-By` trailer or any "Generated with"/"🤖" footer, even
   though the harness's own default PR/commit templates suggest one —
   this project overrides that default. **The commit message's first
   line should follow Conventional Commits** (`feat:`, `fix:`, `feat!:`/
   a `BREAKING CHANGE:` footer for anything actually breaking) when the
   change is more than a patch — since ADR-0010, this is what CI reads
   to decide the next published image version, not just a style
   nicety. Unprefixed/other messages still work and just default to a
   patch bump, so this is a should, not a hard gate — but treat it as
   real signal, not decoration.
3. **Use multiple parallel agents where work naturally decomposes** —
   planning, executing, and testing each other's work — rather than
   doing everything serially in one thread, when a task splits into
   genuinely independent pieces.
4. **`.claude/` files are tracked in git**, not gitignored.
5. **`.env.example` is committed; `.env` (the real local file) is
   gitignored** and is what you actually edit for local dev — don't
   recreate `.env.example` as if it were the working config.
6. **Real authentication is deliberately deferred to the very last
   slice.** Be precise about what that means, because an earlier version
   of this file was not: there is **no** `AuthProvider` class and no RBAC
   scaffolding. What exists is `app.dependencies.get_current_actor`,
   which returns a fixed `unauthenticated` `Actor` so audit events have
   an actor to record. Every endpoint, writes included, is open to anyone
   who can reach the Route. Do not wire up real auth unless the user
   explicitly asks for it — they've confirmed this deferral more than
   once, most recently mid-collector-work ("lets leave the auth for now
   what else is there to make this production and really run?").
7. **Every time you add or edit a file, run the full local check before
   calling the work done — not just a lint pass.** CI gates on `ruff
   check .` *and* `ruff format --check .` *and* `ty` as three separate
   steps (`.github/workflows/ci.yml`'s `lint` job); running only `ruff
   check` and skipping `ruff format --check` has already shipped a commit
   that failed CI on formatting alone even though lint and types were
   both clean. Run the real gate locally, on every touched file, before
   considering a change finished:
   `uv run ruff check . && uv run ruff format --check . && uv run ty check backend/app tools`
   (add `cd frontend && npm run lint && npm run typecheck && npm run build`
   for any frontend change). If `ruff format --check` fails, run
   `uv run ruff format .` and re-verify — don't hand-fix formatting.

   **The type checker is ty, not mypy** — mypy was removed on 2026-09-01
   after being measured against it (`docs/adr/0019-ty-replaces-mypy.md`,
   which has the numbers and the rollback triggers). Three things follow
   that a session used to mypy will get wrong:

   - **Suppressions are `# ty: ignore[rule-name]`.** ty honours a *bare*
     `# type: ignore` but not a coded one — it does not know mypy's rule
     codes — so `# type: ignore[return-value]` silently suppresses
     nothing. See `app.infrastructure.singleflight` for the only one.
   - **Annotations are enforced by ruff, not by the type checker**
     (`ANN001`–`ANN206`, not `ANN401`). ty has no `disallow_untyped_defs`
     and cannot grow one — it infers unannotated bodies rather than
     rejecting them — so this ratchet is the only thing keeping every
     function annotated. It covers `tests/` too, which ty does not.
   - **ty is beta, on 0.0.x, and pinned exactly** for that reason.
     A new diagnostic after a version bump is ty changing, not a
     regression in this codebase. Trust `ty check` over ty's published
     rules reference — they have disagreed about default rule severities,
     which is why `[tool.ty.rules]` writes the reasoned ones down.
8. **Explanation lives in docs, not in the code. Every function gets a
   Google-style docstring.** Added 2026-08-18, and it *reverses* how this
   repo was written up to that date: earlier sessions justified every
   non-obvious choice in inline `#` comments, which grew into walls of
   prose between statements that the user reported as actively hard to
   read. Convention 1 is unchanged — decisions still have to be
   researched and justified — but the justification belongs in
   `docs/` (an ADR for a decision, `docs/cisco-collectors.md` for
   verified implementation facts), with the code carrying at most a
   one-line pointer to it.

   The required docstring shape, on every function, method and class:

   ```python
   def get_user(user_id):
       """
       Get a user by ID.

       Args:
           user_id (str): The ID of the user.

       Returns:
           User: The matching user object.
       """
   ```

   Use `Args:` / `Returns:` / `Raises:` / `Yields:` as they apply
   (an async generator documents `Yields:`, not `Returns:`), give each
   argument its type in parentheses, and skip `self`. A function with no
   arguments and no return value still gets the summary line.

   Inline `#` comments survive only to pin one line's non-obvious
   behaviour where a docstring would be the wrong place — a couple per
   file, not a running commentary. A `# ponytail:` marker is exempt: it
   is tracked debt, not explanation, and `/ponytail-debt` harvests it.

   **Never delete a hard-won fact to satisfy this rule.** Facts like
   "UCSPE 4.2 reports `access='unspecified'` on a blade's own `mgmtIf`"
   cost a live-hardware run to learn, and dropping one silently
   re-opens a fixed bug. Move it to `docs/` with its provenance intact —
   a fact without its source becomes folklore nobody dares change.

   Applied so far to `app.infrastructure.providers.ucs_common`,
   `.ucs_manager` and `.ucs_central`, and to everything written since —
   `.intersight`, `.redfish`, `.openmanage`, `.oneview` and
   `.fake` — plus `tools/verify_*.py`. The older parts of the
   codebase still read in the previous style; convert a file when you are
   already changing it, not as a sweep of its own.

9. **The release notes are the commit subjects — so write the subject
   for whoever deploys it.** Changed 2026-09-05 at the user's request;
   this *replaces* the hand-maintained `CHANGELOG.md`, which is deleted.

   Releases are unattended: every push to `main` that passes CI tags the
   commit and publishes both images, with the version derived from
   Conventional Commits (ADR-0010). A file someone has to remember to
   edit never survives that, and this one did not — nobody is present at
   the moment a version is cut, so its `## Unreleased` heading was never
   renamed and entries sat under it for six releases, telling operators
   to act on changes they already had.

   CI's `Publish the release notes` step now reads the same commit
   subjects the version number comes from, groups them under
   `### Breaking` / `### New features` / `### Fixed` / `### Performance`
   / `### Documentation`, and attaches them to the GitHub Release. The
   notes therefore cannot drift from the release, and there is nothing
   to keep current as you work.

   What that asks of you, in the commit message itself:

   - **The subject line is the release note.** `fix: correct the thing`
     is a wasted line in a document operators read. Name the environment
     variable, the endpoint, the exit code, the Helm value.
   - A `!` (`feat!:`, or a `BREAKING CHANGE:` footer) both bumps the
     major and files the line under `### Breaking`. Say what an operator
     has to *do* — including "nothing, the default is unchanged" when
     that is true, in the body.
   - `refactor`, `test`, `chore`, `style` and `ci` are dropped from the
     notes on purpose: real work, but nothing an operator can observe.
     Use them, and do not dress an internal change as a `feat:` to make
     it appear.
   - The body is still worth writing. It does not reach the release
     notes, but it is what the next session reads from `git log`.

## Current status

Phase 1 slices 0–7 are done (see `docs/architecture.md`'s "What's
implemented vs. planned" section for the full per-slice writeup):
inventory + search/pagination + UI, classification engine, health policy
engine, maintenance + audit trail, classification/health admin UIs, a
10k/50k performance pass, and Playwright E2E coverage.

Beyond the numbered slices, the **first real vendor collector — Cisco UCS
Manager** — is built, and has now been **validated end to end against a
live Cisco UCS Platform Emulator** (UCSPE 4.2(2aS9)): full collector run,
then the REST API and UI over the result.
`docs/adr/0009-ucs-manager-collector.md` records what that proved, what
it disproved, and what it still could not settle. Several defects it
found would have been invisible without real hardware — a nonexistent MO
class that aborted every run, a BMC filter that matched nothing, a whole
class of adapter interface never collected, fabric path counts that were
always zero, and servers named after their chassis slot rather than
their service profile (which silently defeated both site parsing and
classification).

Also since: vendors are a closed enum and sites a closed set loaded
from configuration, a server's site is parsed
from its own name, vendor manager connections come from environment
configuration rather than MongoDB documents plus mounted secrets, and the
UI was rebuilt around a per-site overview as the landing page — which now
leads with three fleet-wide cards (across all sites, UPI, hosted cluster)
above the per-site ones, summed from a `by_installation_type` object
`GET /api/v1/sites` returns per site row.

**Every planned vendor collector now exists.** Cisco Intersight
(ADR-0017), Dell OpenManage (ADR-0020) and HPE OneView (ADR-0022) all
shipped after UCS, alongside `REDFISH_STANDALONE` for machines no
aggregator owns. Two of them — `INTERSIGHT` and `ONEVIEW` — have never
had their field mappings run against live hardware, which is a different
state from every collector before them and is the outstanding action on
the repo. Three platform-wide changes landed with that work and are worth
knowing before reading any collector: a **built-in GPU catalog**
(ADR-0021) fills in VRAM no vendor API reports, **`Server.unread_fields`**
records what a collection could not read, and **every collector now
reports power supplies**, so the health engine's `power.*` metrics
finally have something to read. All three are in "Key technical facts"
below.

The supply-chain pass after that (`docs/adr/0013`) SHA-pinned every CI
action, replaced the release-tagging action before Node 20 removal breaks
it, moved the base image from UBI 9.4 to 9.8, and removed
`python-multipart` — an unused direct dependency carrying seven CVEs.
`pip-audit` and `npm audit` are both clean as of that commit. **It also
left a standing obligation: see "Keeping CI current" below.**

### The collector architecture (read this before touching a collector)

There is no single sync process. Each hardware vendor gets its own
`ServerInventoryProvider` implementation
(`app.infrastructure.providers.<vendor>`, following the seam
`app.domain.ports.provider` defines and `app.infrastructure.providers.
fake` — the Phase-1 synthetic-data provider — already exercises), and
each manager *type* gets its own Kubernetes `CronJob` running
`tools/run_collector.py --manager-type <TYPE>`. A run:

1. Resolves that type's endpoint + login from settings via
   `app.infrastructure.credentials.env.EnvConnectionResolver`
   (`INVENTORY_UCS_CENTRAL_IP`/`_USERNAME`/`_PASSWORD`, same shape for
   `ONEVIEW`, `OME`, `INTERSIGHT`). **One endpoint and one login per
   manager type — that is the whole connection config.** There is no
   `Manager` document to create and no credentials directory to mount;
   both were removed. A half-configured vendor raises
   `ManagerNotConfiguredError` naming the missing variables.

   **`UCS_MANAGER` is the one carve-out: a login with no endpoint.**
   `INVENTORY_UCS_MANAGER_USERNAME`/`_PASSWORD` exist,
   `INVENTORY_UCS_MANAGER_IP` does not, and there is no UCS Manager
   collector to run. The UCS Central collector discovers every domain's
   address from Central at runtime and logs into each one with that
   account, so an endpoint here would name a single domain that nothing
   reads. See the Cisco section below.
2. Talks to the vendor API, normalizes into `ProviderServer`.
3. Runs that through `app.application.services.ingest.IngestService` —
   the exact same pipeline the fake-data seeder and every other
   provider use: classify, health-evaluate, audit, upsert, one write per
   server.

A `Manager` document is still written on each run, but it is a
*projection* of that configuration (`tools.run_collector.manager_for`)
so the API can resolve `Server.manager_id` to something readable — never
its source. Intersight reuses the same three fields with different
meanings: it signs requests with an API key, so `username` is the API Key
ID and `password` the secret key.

A collector never talks to the FastAPI process; the API never talks to a
vendor manager. MongoDB is the only thing connecting them. See
`README.md`'s diagram and `docs/adr/0009-ucs-manager-collector.md`, whose
validation sections record what a live UCS Platform Emulator proved,
disproved and could not settle.

**Five collectors exist: `UCS_CENTRAL` (the UCS-managed Cisco fleet),
`INTERSIGHT` (Cisco servers no UCS domain owns), `OPENMANAGE` (Dell),
`ONEVIEW` (HPE) and `REDFISH_STANDALONE` (every machine no aggregator
owns).** Every `ManagerType` now has an entry in
`tools/run_collector.py`'s `_PROVIDER_FACTORIES` **except `UCS_MANAGER`,
whose absence is deliberate rather than pending** — it is reached through
`UCS_CENTRAL`, which discovers each domain's address at runtime, so there
is nothing to point a CronJob at. The tool says exactly that rather than
reporting an unimplemented feature.

**`INTERSIGHT` is the first collector that actually reaches the 10,000
target**, and the first with three properties nothing else here has —
read `docs/adr/0017-intersight-collector.md` before touching it:

1. **It is not a login.** Intersight has no username/password path for
   its REST API at all; every request is signed (HTTP Signature
   `hs2019`). Its credential variables are named for what they are —
   `INVENTORY_INTERSIGHT_API_KEY_ID` and `_API_KEY_PEM`, not the
   USERNAME/PASSWORD pair every other vendor takes. The PEM rides in the
   environment variable — the signing library takes the key as a string,
   so there is **no key file to mount** and ADR-0012's rule holds.
   Signing is hand-rolled on `httpx` + `cryptography` rather than using
   the official SDK, which is a 57.6 MB wheel of 10,112 generated model
   modules for the eight we would touch. The RSA construction was
   verified byte-identical against that SDK.
2. **Its cost is flat in fleet size.** Every child managed object carries
   an inverse reference to its owner, so each sub-resource is listed once
   for the whole estate and joined in memory — ~120 requests for 10,000
   servers. The trade is memory: the join tables are held for the length
   of the run and scale with the fleet, which no other collector's do.
   `$select` on every query is what keeps that affordable, not a
   micro-optimisation.
3. **It deliberately does not collect `ManagementMode == UCSM`.** Those
   are exactly the servers `UCS_CENTRAL` already owns, and since
   `IngestService` correlates on `(vendor, serial_normalized)`,
   collecting both would make one document's `source_provider` and every
   mapped field flip on whichever CronJob ran last.
   `INVENTORY_INTERSIGHT_MANAGEMENT_MODES` overrides it, for an estate
   whose UCS domains are not registered with Central at all.

**It has never been run against a live Intersight.** The DevNet sandbox
went offline 2026-08-01 with no committed return before ~Q1 2027, and
there is no downloadable emulator equivalent to UCSPE, so everything was
built against the OpenAPI contract as rendered by the installed SDK's
generated models. `TotalMemory` carries **no documented unit anywhere**
and is assumed MiB; if that is wrong every server's memory is 4.86% high,
silently. `uv run python -m tools.verify_intersight` settles it in one
query by summing a real server's DIMMs — run it before scheduling
anything, and record the result in ADR-0017.

An air-gapped site reaches Intersight **only** through an on-prem
Intersight; `intersight.com` is public internet and a *Connected* Virtual
Appliance still calls home. The user has one reachable from the
air-gapped environment (not the flavour Cisco brands a "Private Virtual
Appliance" — the product ships under several names). **So this collector
is testable there, and its first real run is the outstanding action**:
`docs/field-test-checklist.md` says exactly what to run and what to bring
back — three exported variables and `uv run python -m
tools.verify_intersight`. The `TotalMemory` unit is the answer to look
for.

**`OPENMANAGE` (Dell) is the one collector that reads from two places on
purpose**, and the split is on *provenance*: two bulk calls to the
OpenManage Enterprise appliance say which servers exist and what the
operator named them, then each server's own iDRAC is read over Redfish
(reusing `..providers.redfish`, not a second mapping) for the measured
hardware. It is therefore the only collector needing two logins —
`INVENTORY_OME_USERNAME`/`_PASSWORD` plus
`INVENTORY_OME_BMC_USERNAME`/`_PASSWORD` for a shared read-only iDRAC
account — and it refuses to start without both, naming the variables.
See `docs/adr/0020-dell-identity-from-ome-hardware-from-redfish.md` and
`docs/dell-collectors.md`.

**`ONEVIEW` (HPE) deliberately does *not* copy that split, and this is
the thing a future session is most likely to get wrong.** The estate runs
iLO 4, 5 and 6 in the same racks, and iLO 4 predates useful Redfish
coverage. A Dell-shaped design would therefore mean a per-generation
branch in the collection path and two different sets of field provenance
for one vendor's servers in one inventory — "why does this server have
thread counts and that one doesn't" becomes a question about which branch
ran. **So the user decided, explicitly, on one collection standard for
all HP hardware: OneView, for every server, whatever its iLO
generation.** It is not up for re-litigation in code. Concretely: no
Redfish pass, no `RedfishTarget`, no BMC credentials, no
`INVENTORY_ONEVIEW_BMC_*`, and `mpModel` is read and *reported* but never
branched on. What OneView cannot report is `None` — "not read this run" —
never zero. Read `docs/adr/0022-oneview-only-hpe-collector.md` and
`docs/hpe-collectors.md` before touching it.

The cost is three bulk calls per appliance — `GET /rest/server-hardware`
returns the *complete* object per member rather than a summary, and
`expand=all` folds in each server's DIMMs, drives, GPUs and PCI devices.
The one exception is power supplies, which cost a request per server
(`INVENTORY_ONEVIEW_COLLECT_PSUS`, on by default,
`INVENTORY_ONEVIEW_PSU_CONCURRENCY` bounding the fan-out).

**Like Intersight, it has never been run against live hardware**, and
unlike UCS there is nothing that could change that from this repo: HPE's
60-day OneView trial is a *real appliance*, not a hardware simulator, so
with no HPE hardware attached it enumerates nothing. `uv run python -m
tools.verify_oneview` — read-only, writes nothing, logs out — is the
outstanding action, and `docs/field-test-checklist.md` (part 2) says what
to run and what to bring back. Its headline answer is whether
`processorCount * processorCoreCount` is the real core count; the
highest-consequence one is whether `/rest/server-profiles`' 256 cap is
per request or per query, because the *name* comes from the profile.

### What's explicitly NOT done yet (in rough priority order the user has confirmed)

0. **Staleness detection**, for every collector rather than only Redfish
   now that five CronJobs exist. A CronJob pod is
   never scraped by Prometheus, so no collector-side metric can report
   its own absence — the only thing that can answer "40 hosts have been
   failing for two weeks" is the API exposing gauges derived from
   MongoDB's `last_seen_at` (written on every ingest, currently read by
   nothing). Until that lands, staleness is the manual query in
   `docs/test-redfish-standalone-collector.md` §6.
1. **Live-hardware validation of the two unproven collectors.** Every
   vendor collector is now *written* — Dell (ADR-0020) and HPE
   (ADR-0022) both shipped, so "build the next vendor collector" is no
   longer on this list. What is left is proof: `INTERSIGHT` and
   `ONEVIEW` have never had their field mappings run against real
   hardware, and UCS's own emulator run found five defects that were
   invisible without it. `uv run python -m tools.verify_intersight` and
   `uv run python -m tools.verify_oneview` are the two commands;
   `docs/field-test-checklist.md` has both, with what to send back.
   Record what each settles in its ADR.

   **The research bar for any future vendor work is unchanged**, so it
   is kept here rather than deleted with the item it belonged to:
   research that vendor's *current* API docs directly, and don't trust
   this file's or any older research's specifics without reconfirming
   them. UCS Manager's build read Cisco's official XML API guide and
   cross-checked every attribute name against the actually-installed
   `ucsmsdk` package source rather than trusting documentation
   summaries; OneView's read HPE's API Reference and the `hpeOneView`
   SDK's source for the four behaviours a hand-rolled client learns the
   hard way. Hold that bar. Testability without real hardware varies a
   lot by vendor and was the deciding factor for going UCS-first — two
   of the four vendors turned out to have no test target at all, which
   is why this item exists.
2. **Remaining deployment/CD gaps**, explicitly deferred by the user in
   favor of collectors: CI now builds and publishes both images to GHCR
   on every push to main (`.github/workflows/ci.yml`'s `publish` job,
   `docs/adr/0010-image-publishing-and-versioning.md`), versioned
   automatically from Conventional Commits — but nothing *deploys* those
   images anywhere yet (no GitOps/ArgoCD wiring, no automatic manifest
   update). No Kubernetes manifests exist for the frontend
   (only the backend API has a Deployment/Route, despite the frontend
   having a solid Containerfile since slice 1 — see `deploy/README.md`);
   the `INVENTORY_CURSOR_SECRET` insecure default is only a code
   comment, not enforced at startup; no rate-limiting middleware
   anywhere; Mongo HA/backup and Redis persistence are explicitly
   documented as "the platform's problem" but nobody has actually stood
   either up; no alerting rules or dashboards on top of the Prometheus
   metrics that already exist.
3. **Real authentication** — the release gate, explicitly last. There is
   no permissive `AuthProvider` to swap out (convention 6 above says why
   an earlier version of this file was wrong about that): what exists is
   `app.dependencies.get_current_actor` returning a fixed
   `unauthenticated` `Actor`, so building this means introducing the
   concept, not replacing one. It touches every router.

## Key technical facts worth knowing before you change something

Full detail lives in `docs/adr/`; this is just the index of what's
non-obvious enough to bite you.

- **A server's site is parsed from its name**
  (`app.domain.value_objects.site.parse_site_code`), never taken from
  configuration — `ocp4-prod-tlv-infra-01` -> `tlv`. Token-based, not a
  substring search (`ocp4-tlvx-01` contains "tlv" but names no site),
  and an ambiguous name yields `None` rather than a guess. `None` is a
  real state the UI shows as "Unassigned". A code spelled with a
  separator (`bat-yam`) matches consecutive tokens.

  **Which sites exist is `INVENTORY_SITES`, not code** (ADR-0018).
  `SiteCode` is gone; the set is a `SiteCatalog` parsed from
  `"nyc:New York City,tlv:Tel Aviv,bat-yam:Bat Yam,five:Site Five"` —
  that string is the shipped default, and an estate sets its own. The
  set is still *closed*, just closed at runtime, and it is still the
  server's own name that picks from it. Three things follow. The catalog
  is threaded explicitly (`IngestService(sites=...)`,
  `parse_site_code(name, catalog)`, `default_system_rules(catalog)`) —
  the domain never reads `Settings`. `Server.site_id` is a plain `str`,
  deliberately, so a document written before a site was renamed away
  still loads. And `INVENTORY_SITES` lives in the shared `api-config`
  ConfigMap because the API *and* every collector must agree on it — a
  collector derives each server's site at ingest. **A Cisco server whose name carries no site
  token falls back to its service profile's org DN**
  (`org-root/org_tlv/ls-worker-01` -> `tlv`) — the name is still the
  authority, the org path is only consulted when it says nothing.
- **`Vendor` is dell/cisco/hp/standalone — there is still no `UNKNOWN`.**
  `STANDALONE` means *a manufacturer this platform does not model*
  (Lenovo, Supermicro, a whitebox) **or one the BMC did not report at
  all** (`ComputerSystem.Manufacturer` absent/null maps to `STANDALONE`
  too, since 2026-08-23 — a deliberate reversal of the original
  fail-the-system design, accepting a real correlation-key risk to keep
  every listed BMC ingested; see ADR-0016's dated update), **not**
  "collected without a manager": a Dell reached at its own BMC is still
  `dell`, because `IngestService` correlates on `(vendor,
  serial_normalized)` and moving a machine between vendors splits it
  into two documents. Which collector found a server is
  `Server.source_provider`, which is filterable.
- **A provider reports `None` for a field it could not read**, which is
  not the same as zero or empty. `IngestService` carries the stored value
  forward for a `None` and overwrites for a real value. Before this
  existed, a sub-resource that 404'd wrote zeros over good data — which
  took a server from CRITICAL to HEALTHY by reporting no drives, and
  logged an audit event saying the drive had recovered.
- **`Server.unread_fields` says which fields that was**, and directly
  extends the rule above. It is a list of dotted API paths into the
  server's own response (`hardware.gpus`, `hardware.storage.drives`,
  `hardware.power.psus`, `identity.nic_macs`, …) that the *most recent*
  collection could not read, built by `IngestService._carry_forward`,
  returned by `GET /api/v1/servers/{id}`. Two properties are load-bearing:
  it is **recomputed from scratch every ingest and never merged** (a path
  whose value is no longer `None` would otherwise stay flagged forever),
  and **"never successfully read" is deliberately not expressible** — the
  question it answers is about this run. It exists because carrying
  forward is not enough on a *first* ingest: `Hardware` has no "unknown"
  state, so an iLO-4 server that reported nothing stored `0` drives and
  rendered as a confident, real zero.
- **GPU VRAM comes from a built-in catalog, not from any vendor API.** No
  management plane this platform collects from reports a GPU's memory
  size — confirmed against both Cisco SDKs, Cisco's own metrics API,
  Redfish and OneView — so `app.domain.value_objects.gpu_catalog` ships a
  table of 30 NVIDIA and AMD datacenter cards
  (`gpu_models.DEFAULT_GPU_MODELS`) and `IngestService` enriches from it.
  **`INVENTORY_GPU_MODELS` overrides that table per identifier; it is no
  longer the only source, and an empty value no longer means "enrich
  nothing"** — that reversal is `docs/adr/0021-built-in-gpu-catalog-with-
  model-matching.md`, which supersedes the "deliberately not a hardcoded
  table" reasoning in `docs/cisco-collectors.md`. Three rules matter if
  you touch it: a card is matched on a **Cisco PID *or* a normalized
  model string** (`NVIDIA A100-PCIE-40GB`), because no Redfish or OneView
  GPU reports a PID at all; **the comparison is equality on that
  normalized key, never a substring or fuzzy match** (`A10` vs `A100` is
  one character and 3x the VRAM); and a model that shipped in two
  capacities (`A100`, `V100`, `H100`, `P100`) has **no bare-name row**, so
  it matches nothing and keeps `memory_bytes: None` rather than guessing.
  A value a provider actually read is never overridden. Only add a row
  whose VRAM you can cite from a vendor datasheet or a Cisco spec sheet.
- **Every collector reports power supplies now**, so the health engine's
  `power.psu_count`/`power.failed_psu_count` metrics finally have
  something to read (they had nothing until 2026-09; a server with a dead
  PSU reported HEALTHY on power exactly like one with two good ones).
  Intersight and UCS Manager/Central cover rack units only — a blade's
  supplies belong to its shared chassis, not to the blade — while
  `..redfish.mapping.psus_from_supplies` covers Dell and every standalone
  BMC, and OneView covers HPE with the richest data of the four. Two rules
  are shared by all of them: **an `Absent` supply is dropped, never
  counted as failed** (a four-bay chassis with two fitted is not two
  failed PSUs), and **a PSU's `health` is `UP`/`DOWN`/`DISABLED`/
  `UNKNOWN`, never a `HealthSeverity`** — a policy against
  `power.failed_psu_count` compares to `"DOWN"`, not `"FAILED"`. Redfish's
  `Warning` maps to `UNKNOWN` rather than `DOWN` on purpose: a degraded
  supply still delivering power has not lost redundancy.
- **HPE's traps, all of which cost real research** — full detail in
  `docs/hpe-collectors.md`, the decisions in ADR-0022:
  - **The name comes from the server profile.** `server-hardware.name` is
    the enclosure-and-bay location and `serverName` is an OS hostname via
    HPE's Agentless Management Service — both decoys, the same trap
    ADR-0009 records for UCS blades named after their chassis slot.
    Hardware with no assigned profile is **skipped**, counted and logged.
  - **`processorCoreCount` is per processor**, so `cpu_cores` is
    `processorCount * processorCoreCount`. Unmultiplied, every two-socket
    server is half its real core count, silently.
  - **`memoryMb` is MiB**, and HPE says so inline — no assumption, unlike
    Intersight's `TotalMemory`.
  - **`count=-1` means 64, not "all"** on `/rest/server-profiles`, with a
    256 ceiling and truncation HPE documents without saying whether
    paging passes it. An explicit `count` is always sent, and a short read
    logs `oneview.collection_truncated` at ERROR.
  - **`InsufficientFirmware` is "could not read", not zero.** Every
    subresource on an iLO-4 server fails that way; only
    `collectionState == "Collected"` yields data, and everything else —
    `CollectedStale` included — maps to `None`.
- **A collector only ingests servers whose name matches
  `INVENTORY_COLLECTOR_NAME_PATTERN`** (`^ocp` in `.env.example` and
  `values.yaml`; empty = collect everything). A vendor manager holds the
  whole datacenter, and the name is the only thing distinguishing this
  platform's fleet. Applied as a `_NameFilteredProvider` wrapper in
  `tools/run_collector.py`, not inside `IngestService` — collection scope
  is the collector's concern, the seeder shouldn't inherit it, and
  `--dry-run` bypasses `IngestService` on purpose so a filter there would
  make dry runs lie. A non-matching server is never fetched: no document,
  no health state, no audit trail. This is **not** the UPI-vs-hosted
  distinction — that's classification rules over what *is* collected.
  **`REDFISH_STANDALONE` is exempt** (`_UNFILTERED_TYPES`): a BMC does not
  know the server's `ocp4-...` name, so the pattern would discard every
  host the operator listed. Its inventory file is the filter instead.
- **A collector's whole connection config is env** — one endpoint and
  login per `ManagerType`. No `Manager` document is read to decide where
  to connect and there is no credentials directory; see the collector
  architecture section above.
- A UCS server's name comes from its **service profile**, not
  `computeBlade.name`, which is empty in practice. Getting this wrong
  names every server after its chassis slot, which carries neither a
  site token nor a classifiable pattern.
- Every repository stores `datetime` fields as ISO 8601 **strings**
  (`model_dump(mode="json")`), never native BSON dates. Any range/cursor
  query must compare against that stored string type, not a parsed
  `datetime` — this caused a real, silent-wrong-results bug once
  (`docs/adr/0006`).
- MongoDB is the sole source of truth; Redis is cache-aside only and
  every read path degrades to Mongo on any Redis failure — never make
  Redis a hard dependency for correctness.
- Pagination is keyset (HMAC-signed cursor for `/servers`), never
  `skip`/`offset`.
- Sites from configuration rather than an enum is `docs/adr/0018`,
  which supersedes part of `0011`.
- Sites/vendors as closed sets, name-derived sites and the UI rebuild are
  `docs/adr/0011`; env-based manager connections and the single manifest
  set are `docs/adr/0012`; CI action pinning, the removed Dependabot and
  the manual-maintenance obligation are `docs/adr/0013`.
- Health-policy override/shadowing (`policy_key` families) is the
  platform's headline design decision — read `docs/adr/0005` before
  touching anything in `app.domain.services.health`.
- `ucsmsdk` (and any future vendor SDK) is very likely synchronous —
  wrap blocking calls in `asyncio.to_thread`, never call them directly
  from an async context (`app.infrastructure.providers.ucs_manager.
  client`).
- `requirements.txt`/`pylock.toml` at the repo root are generated
  exports for air-gapped mirroring — regenerate both after any
  `pyproject.toml` dependency change:
  `uv export --format requirements-txt --no-dev --no-emit-project -o requirements.txt`
  and the `pylock.toml` equivalent (see `docs/air-gap.md`).
- Frontend E2E (`frontend/e2e/`, Playwright): a real Chromium quirk means
  `getByLabel` collides across sibling `<select>` fields on the
  classification-rule/health-policy editor pages — use the `labeledField`
  helper in `frontend/e2e/helpers.ts`, not `getByLabel`, for anything
  wrapping a `<select>` (`docs/adr/0008`).

## Verifying your work

```bash
scripts/dev-up.sh up                              # Mongo + Redis (podman/docker)
uv sync --all-groups && cp .env.example .env       # first time only
uv run python -m tools.seed_inventory --count 1000 --seed 42

uv run pytest -q                                   # backend: unit + integration + api
uv run ruff check . && uv run ruff format --check . && uv run ty check backend/app tools

cd frontend && npm run lint && npm run typecheck && npm run test -- --run && npm run build
npm run test:e2e                                    # needs backend + frontend dev server running
```

For a real test of the UCS Manager data path (which the Cisco collector
drives per domain) without production hardware:
Cisco's UCS Platform Emulator (UCSPE) is a free, downloadable VM (Cisco.com
login only, no support contract) that runs the actual UCS Manager binary
against simulated hardware and answers real XML API calls — see
`docs/adr/0009` for what's confirmed vs. still assumed about the mapping,
and validate against UCSPE (or real hardware) before trusting this in
production.

**A known local-sandbox gotcha, not a code bug**: in some CLI sandbox
environments, rootless Podman containers get reaped between separate
shell commands because the user session has no working `systemd`
linger (`loginctl show-user $(whoami) | grep Linger` shows `no`, and
`loginctl enable-linger` fails with "No such device or address" — no
`systemd-logind` D-Bus session to talk to). If `scripts/dev-up.sh up`
reports success but a subsequent command can't reach Mongo, that's very
likely this — check `podman ps` before assuming a real regression. Real
CI (GitHub Actions) does not have this problem; it gets fresh, real
service containers per run.

## Keeping CI current (a standing chore, not a one-off)

Every action in `.github/workflows/ci.yml` is pinned to a commit SHA, so
**nothing updates itself**. Dependabot was tried and deliberately removed
(`docs/adr/0013` explains why), which makes this a manual pass — roughly
quarterly, or before any release you care about:

1. **Are the pins current?** For each `uses:` line, compare the trailing
   `# vX.Y.Z` comment against the action's latest release. Verify the new
   tag actually resolves before pinning it — this repo has been broken
   twice by assuming a rolling major tag exists (`github-tag-action` has
   no `v6`; `setup-uv` has no `v8`/`v9`/`v10`).
2. **Is anything vulnerable?**
   `uv run --with pip-audit pip-audit --skip-editable` and, in
   `frontend/`, `npm audit`. This is a different question from step 1 —
   the `python-multipart` finding was a *direct* dependency that no
   version-bump tooling had flagged.
3. **Is anything unused?** The fix for that finding was deletion, not an
   upgrade. Check whether a vulnerable package is actually reached before
   bumping it.
4. **Any runtime deprecations?** Actions declare a Node version
   (`using: node20`). GitHub removes old ones on a schedule, and an
   unmaintained action can have no upgrade path at all — that is what
   forced the tagging-action replacement in ADR-0010.
5. **Base images:** `Containerfile` pins `ubi9/ubi-minimal` to a minor
   stream (9.8). Check for a newer 9.x.
6. **ty:** pinned to an exact `0.0.x` (`ty==0.0.76`), because it is beta
   and Astral state that diagnostics may change between any two
   releases. Check for a newer release on this pass — and when you bump
   it, **expect the diagnostics to move, and read a new error as ty
   changing rather than as a regression in this codebase**. Two
   corollaries, both learned the hard way in ADR-0019: ty's published
   rules reference has disagreed with the shipped binary about default
   rule severities, so trust `ty check` over the docs; and because ty
   resolves types from *installed source*, a dependency bump can change
   its output with no change to our code at all.

## Where to continue right now

The most recent user direction was: real vendor collectors first,
deployment/CD gaps and auth deliberately parked. **Every planned vendor
collector now exists**, so the phase that direction described is finished
in code and unfinished in proof. The natural next steps:

1. **Run the two probes against real hardware.** This is the highest-value
   action on the repo and it is not more code.
   `uv run python -m tools.verify_intersight` against the on-prem
   Intersight (the user has one reachable from the air-gapped
   environment) and `uv run python -m tools.verify_oneview` against the
   OneView appliance. Both are read-only.
   `docs/field-test-checklist.md` is the operator-facing version of both
   errands. Record what each settles in ADR-0017 / ADR-0022 rather than
   only in a chat reply — a result nobody wrote down is a result the next
   session re-derives.

   For Intersight the answer to look for is the `TotalMemory` unit and a
   full `--dry-run` ingest (auth, name resolution and the MiB assumption
   were confirmed on 2026-09-01; the rest of ADR-0017's UNVERIFIED list
   was not). For OneView it is the core-count check and whether paging
   gets past the 256-profile ceiling.

2. **Give the Dell collector a seeded shape.** The UI half of this item
   is done: `SOURCE_PROVIDERS` now lists all five collectors, and the
   guard that was supposed to catch its drift no longer *restates* the
   set of implemented collectors — it derives it from
   `tools.run_collector.PROVIDER_FACTORIES`, which is the one source of
   truth. That is why the drift was invisible: the guard had drifted
   along with the list it guarded and stayed green.

   What is left is the seeder. `COLLECTOR_TYPES` shapes four of the five,
   and `tests/unit/infrastructure/providers/test_generator.py`'s
   `_UNSEEDED_COLLECTORS` now names `OPENMANAGE` as a deliberate,
   documented exclusion rather than an oversight — so a *sixth* collector
   with no shape fails that test, but Dell's stays a known gap. It is not
   a list entry: a Dell server collected through OME is read over
   Redfish, so `provider_type_for` cannot tell it apart from a
   `REDFISH_STANDALONE` Dell by `external_id` prefix, which is the
   discriminator every other collector uses. Seeding it means carrying
   the collector on the generated server instead of reading it back off
   `external_id`.

3. **UCS's own leftovers, still open** and still only settleable on real
   hardware: the `total_memory` MB assumption (UCSPE reports one
   synthetic value for every model and contradicts itself elsewhere), a
   fully *associated* service profile (the emulator's stopped at
   `config-failure` for want of a boot policy, vNICs and a UUID pool),
   and ADR-0009's original scope cuts — CPU model string, per-drive
   storage detail, fabric interconnect identity. A UCS Central dry run on
   the same trip as step 1 settles the memory question for UCS and
   Intersight at once, which is why `docs/field-test-checklist.md`
   already asks for it.

4. **Then the deployment/CD and auth gaps above**, which are the rest of
   what "production and really run" means for this platform — staleness
   detection first, since it is item 0 of the not-done list and nothing
   else answers "40 hosts have been failing for two weeks". Ask the user
   before assuming this is the next phase; the ordering above is the
   direction they have been steering toward, not a plan they have signed
   off on.
