# CLAUDE.md

This file orients a Claude Code session picking up this repository —
whether that's a fresh session or one resuming after a break. Read this
before making changes. `README.md` is the human-facing quickstart;
`docs/architecture.md` and `docs/adr/*` are the technical deep-dives this
file points into rather than duplicates.

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
   slice.** The `AuthProvider`/RBAC scaffolding exists now (permissive,
   not enforcing), but do not wire up real auth unless the user
   explicitly asks for it — they've confirmed this deferral more than
   once, most recently mid-collector-work ("lets leave the auth for now
   what else is there to make this production and really run?").
7. **Every time you add or edit a file, run the full local check before
   calling the work done — not just a lint pass.** CI gates on `ruff
   check .` *and* `ruff format --check .` *and* `mypy` as three separate
   steps (`.github/workflows/ci.yml`'s `lint` job); running only `ruff
   check` and skipping `ruff format --check` has already shipped a commit
   that failed CI on formatting alone even though lint and types were
   both clean. Run the real gate locally, on every touched file, before
   considering a change finished:
   `uv run ruff check . && uv run ruff format --check . && uv run mypy backend/app tools`
   (add `cd frontend && npm run lint && npm run typecheck && npm run build`
   for any frontend change). If `ruff format --check` fails, run
   `uv run ruff format .` and re-verify — don't hand-fix formatting.
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
   `.ucs_manager` and `.ucs_central`. The rest of the codebase still
   reads in the older style; convert a file when you are already
   changing it, not as a sweep of its own.

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

Also since: sites and vendors are closed enums, a server's site is parsed
from its own name, vendor manager connections come from environment
configuration rather than MongoDB documents plus mounted secrets, and the
UI was rebuilt around a per-site overview as the landing page.

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

**Three collectors exist: `UCS_CENTRAL` (the UCS-managed Cisco fleet),
`INTERSIGHT` (Cisco servers no UCS domain owns) and `REDFISH_STANDALONE`
(every machine no aggregator owns).** `OPENMANAGE` and `ONEVIEW` have
configuration slots but no implementation — `tools/run_collector.py`'s
`_PROVIDER_FACTORIES` raises a clear `NotImplementedError` for them, not
a silent no-op.

**`INTERSIGHT` is the first collector that actually reaches the 10,000
target**, and the first with three properties nothing else here has —
read `docs/adr/0017-intersight-collector.md` before touching it:

1. **It is not a login.** Intersight has no username/password path for
   its REST API at all; every request is signed (HTTP Signature
   `hs2019`). `INVENTORY_INTERSIGHT_USERNAME` is the API Key ID and
   `_PASSWORD` is that key's PEM private half. The PEM rides in the
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

An air-gapped site reaches Intersight **only** through an on-prem Private
Virtual Appliance; `intersight.com` is public internet and a *Connected*
Virtual Appliance still calls home. The user has confirmed a PVA exists
or is planned, which is the premise this collector's deployability rests
on.

**`REDFISH_STANDALONE` breaks three assumptions the rest of this file
states, deliberately and with an ADR each time — read
`docs/adr/0016-redfish-standalone-collector.md` before touching it:**

1. **Its fleet comes from a mounted TOML inventory file, not from env.**
   There is no aggregator to ask what exists. Like `UCS_MANAGER` it has a
   login and no endpoint, but its endpoints are the hosts in that file,
   and each may carry its own credential (referenced by name from a
   mounted Secret). This deviates from ADR-0012's "no secret volume to
   mount"; ADR-0016 names the deviation rather than hiding it.
2. **`INVENTORY_COLLECTOR_NAME_PATTERN` does not apply to it.** A BMC
   does not know the server's `ocp4-...` name, so `^ocp` would discard
   every host the operator listed. The inventory file is the filter, and
   there is no second line of defence behind it — treat it as a
   review-gated, production-critical artifact.
3. **Its cost is per *server*, not per manager** (~25 round trips per
   BMC, against hardware that degrades when polled), so bounded
   concurrency, a per-host wall-clock budget and a total-run budget are
   correctness requirements rather than tuning knobs. Supported range is
   ~400–1000 hosts per CronJob, sharded by inventory directory beyond
   that. **It does not reach the platform's 10k target in this shape**,
   and the ADR says so.

A standalone Cisco CIMC is collected this way. That does **not** restore
a UCS Manager entry point — `--manager-type UCS_MANAGER` remains deleted
(below), and reaching a CIMC over Redfish is a different protocol to a
different endpoint that knows nothing about domains or service profiles.

Building the next collector means: implement `ServerInventoryProvider`
for it under `app.infrastructure.providers.<vendor>`, add it to
`_PROVIDER_FACTORIES`, and add a CronJob template mirroring
`deploy/helm/server-inventory/templates/ucs-central-collector-cronjob.yaml`
(or the `redfish-standalone-` one, if it needs mounted configuration).

**How it works: Central discovers, UCS Manager collects.** Two queries go
to Central regardless of fleet size — `computeSystem` for the registered
domains and their addresses, `lsServer` for profile names and each one's
domain. Everything else is read live from each domain's own UCS Manager
through `..providers.ucs_manager` unchanged, up to
`INVENTORY_UCS_CENTRAL_DOMAIN_CONCURRENCY` domains at once, using
`INVENTORY_UCS_MANAGER_USERNAME`/`_PASSWORD` as the login for every
domain. So `UcsManagerProvider` is not dead code — it is the engine, and
ADR-0009's UCSPE validation is exactly why it was reused rather than
reimplemented against Central's replica.

Two behaviours worth knowing before you touch it. A domain is skipped
**only** when Central lists profiles for it and none match
`INVENTORY_COLLECTOR_NAME_PATTERN` — a domain with no known profiles is
always collected, so an incomplete replica can never silently prune the
fleet. And collected servers get their `external_id` rewritten from the
domain-local `sys/...` (which repeats in every domain) to
`compute/sys-<domainId>/...`, so it identifies one machine and names its
domain.

**The cost, accepted knowingly: a domain not registered with Central is
uncollectable, and Central is a hard single point of failure for all
Cisco collection.** There is no standalone UCS Manager entry point any
more — `--manager-type UCS_MANAGER` was removed along with its CronJob
and `INVENTORY_UCS_MANAGER_IP`. Don't "restore" it as a fix without
asking; it was deleted deliberately.

`docs/adr/0014` has the full evidence trail, including its 2026-08-17
and 2026-08-18 updates. **It is now validated against a live UCS
Central** — 152 registered domains, ~3346 equipped servers, real
`verify_ucs_central` and `run_collector --dry-run` runs. The open
question of whether Central replicates domain-*local* service
profiles — the source of a server's name and hence of site parsing,
classification and the `^ocp` match — is answered for that fleet: it
uses **zero** local profiles; every one is `global-controlled` (owned by
Central itself), which makes Central's `lsServer` copy authoritative by
construction there and settles pruning as safe for it. The SDK schema
still supports `localized` profiles (`LsSPMeta.ownership_state`), and a
fleet that actually uses them remains untested here — run
`uv run python -m tools.verify_ucs_central` against any new deployment
before trusting it there too: read-only, writes nothing, prints a
GOOD/PARTIAL/BAD verdict plus the `ownership_state` breakdown. Update
ADR-0014 with the result. At runtime the provider also logs
`ucs_central.domain_summary` and warns
`ucs_central.domain_without_profiles`.

**Shared Cisco logic lives in `app.infrastructure.providers.ucs_common`**
(`is_equipped`, `group_by_owning_server_dn`, `bmc_interface`,
`partition_profiles`, plus `normalize_oper_state`/`normalize_admin_state`
— the interface-state vocabulary Intersight shares with both UCS SDKs),
and `ucs_manager.mapping` serves both UCS providers.
`ucscsdk` and `ucsmsdk` describe the same object model with the same
attribute names — only the DN root differs — so duplicating any of it
means the next emulator-found fix lands in one copy only. Everything
there works on relative DN structure, never an absolute root.

### What's explicitly NOT done yet (in rough priority order the user has confirmed)

0. **Staleness detection for the Redfish collector.** A CronJob pod is
   never scraped by Prometheus, so no collector-side metric can report
   its own absence — the only thing that can answer "40 hosts have been
   failing for two weeks" is the API exposing gauges derived from
   MongoDB's `last_seen_at` (written on every ingest, currently read by
   nothing). Until that lands, staleness is the manual query in
   `docs/test-redfish-standalone-collector.md` §6.
1. **Dell OpenManage / HPE OneView collectors.** Not started. (Cisco
   Intersight is done — see above.) Before picking one: research each vendor's *current* API
   docs directly (don't trust this file's or any older research's
   specifics without reconfirming) — UCS Manager's build researched
   Cisco's official XML API guide and cross-checked every attribute name
   against the actually-installed `ucsmsdk` package source rather than
   trusting documentation summaries alone; hold the same bar for the
   next vendor. Testability without real hardware varies a lot by
   vendor — that mattered enough to be the deciding factor for going
   UCS-first; check it again before committing to a build order.
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
3. **Real authentication** — the release gate, explicitly last. Swaps
   the current permissive `AuthProvider` for a real one; touches every
   router.

## Key technical facts worth knowing before you change something

Full detail lives in `docs/adr/`; this is just the index of what's
non-obvious enough to bite you.

- **A server's site is parsed from its name**
  (`app.domain.value_objects.site.parse_site_code`), never taken from
  configuration — `ocp4-prod-tlv-infra-01` -> `tlv`. Token-based, not a
  substring search (`ocp4-tlvx-01` contains "tlv" but names no site),
  and an ambiguous name yields `None` rather than a guess. `None` is a
  real state the UI shows as "Unassigned". The sites are `nyc`, `tlv`,
  `bat-yam` and `five`; a code spelled with a separator (`bat-yam`)
  matches consecutive tokens. **A Cisco server whose name carries no site
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
uv run ruff check . && uv run ruff format --check . && uv run mypy backend/app tools

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

## Where to continue right now

The most recent user direction was: real vendor collectors first,
deployment/CD gaps and auth deliberately parked. The natural next steps,
in the order the user has been steering toward:

1. ~~Validate the UCS Manager collector against UCSPE~~ — **done**, and
   it found five real defects (see `docs/adr/0009`'s validation
   sections). What it could *not* settle is still open: the
   `total_memory` MB assumption (UCSPE reports one synthetic value for
   every model and contradicts itself elsewhere), a fully *associated*
   service profile (the emulator's stopped at `config-failure` for want
   of a boot policy, vNICs and a UUID pool), and the original scope cuts
   — CPU model string, per-drive storage detail, fabric interconnect
   identity. Real hardware settles those.
2. ~~Build the Cisco Intersight collector~~ — **done** (ADR-0017), but
   **unvalidated against live hardware**, which is a different state from
   every collector before it. The highest-value next action on it is not
   more code: it is running `tools/verify_intersight.py` against the real
   appliance and recording what it settles.
3. Build the next vendor collector — Dell OpenManage or HPE OneView. Ask
   the user which one before assuming. Their earlier preference was
   "easiest to actually test," which favored UCS's real emulator;
   Intersight was then built with *no* test target at all, so that
   criterion is no longer the only one in play. Re-evaluate the tradeoff
   fresh rather than assuming the same research still holds.
4. Once collectors are further along (or if the user redirects), the
   deployment/CD and auth gaps above are the rest of what "production
   and really run" means for this platform.
