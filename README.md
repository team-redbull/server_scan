# Server Inventory Platform

A production-grade, air-gapped bare-metal server inventory platform:
MongoDB-backed inventory, a FastAPI REST API, a React admin UI, Redis
cache, a regex-based classification engine, and a declarative health-
policy engine — with real vendor collectors landing one at a time behind
a stable seam (see "How data actually gets in" below).

**New to this codebase, including a fresh Claude Code session picking
this up?** Read [`CLAUDE.md`](CLAUDE.md) first — it has the current
status, the standing project conventions, and exactly where to continue.

## Status

Phase 1 is functionally complete except real authentication (deliberately
deferred — see `CLAUDE.md`) and three of the four planned vendor
collectors. Built so far, in order:

1. Inventory model, MongoDB/Redis persistence, search/filter/sort/cursor
   pagination, the inventory UI.
2. Classification engine (regex-based, scope + priority resolution).
3. Health policy engine (declarative conditions, `policy_key`
   override/shadowing).
4. Maintenance windows and an append-only audit trail.
5. Classification-rule and health-policy admin UIs.
6. A 10k/50k-scale performance pass (real index-coverage verification and
   load testing, not just fixture-sized tests).
7. Playwright E2E coverage of the critical admin flows.
8. The first real vendor collector: **Cisco UCS Manager**, deployed as a
   Kubernetes CronJob and validated against a live UCS Platform Emulator.
9. A UI rebuilt around what an operator actually scans for: a per-site
   overview as the landing page, and a three-column server list (name,
   model, state) with everything else on the detail page.
10. The second collector: **standalone Redfish**, for machines no
    aggregator owns — a Cisco CIMC that Intersight cannot manage, an
    iDRAC, a current iLO. One BMC at a time, from an inventory file.

`docs/arc42.md` is the structured architecture overview — goals,
constraints, context, deployment view, quality scenarios, and an honest
risk/technical-debt register. See `docs/architecture.md` for the full
per-slice writeup and `docs/adr/` for the individual design decisions.

## How data actually gets in

This is the one idea worth understanding before anything else: **there is
no single "sync" process.** Each hardware vendor's manager (Cisco UCS
Central, Cisco Intersight, and eventually Dell OpenManage Enterprise and
HPE OneView) gets its own small collector program, and each collector runs as
its own **Kubernetes `CronJob`** — one CronJob per manager *type*, not per
physical manager. On a schedule, a CronJob's pod:

1. Reads that vendor's endpoint and login from configuration — one set
   per manager type (`INVENTORY_UCS_CENTRAL_IP` / `_USERNAME` /
   `_PASSWORD`, and the same shape for Intersight, OneView and OME). No
   `Manager` document to create, no secret volume to mount; in Kubernetes
   these arrive from a `Secret` via `envFrom`. (The UCS Central collector
   needs one thing more, and one thing less: `INVENTORY_UCS_MANAGER_USERNAME` /
   `_PASSWORD` to log into each domain, and no UCS Manager endpoint,
   because Central reports every domain's address itself. Intersight's two
   credential variables are not a login at all: it signs every request
   with an API key, so `_USERNAME` is the API Key ID and `_PASSWORD` is
   that key's PEM private half.)
2. Talks to that vendor's real API and normalizes what it reports into
   the platform's vendor-neutral `ProviderServer` shape
   (`app.domain.ports.provider`).
3. Feeds that through the exact same ingestion pipeline every part of the
   platform already exercises with fake data
   (`app.application.services.ingest.IngestService`) — classify, health-
   evaluate, audit, and upsert into MongoDB, all in one write per server.

A server's **site** is not configured *per manager*: it is parsed from
the server's own name (`ocp4-prod-tlv-infra-01` -> site `tlv`), so a
misconfigured manager cannot mislabel everything it collects. A name with
no site token is surfaced as "Unassigned" rather than defaulted.

**Which sites exist is one environment variable**, because a site code is
a property of your hostname convention rather than of this code:

```bash
INVENTORY_SITES="nyc:New York City,tlv:Tel Aviv,bat-yam:Bat Yam,five:Site Five"
```

`code:Display Name`, comma-separated; the display half is optional.
Changing it renames or adds a site across the API, the site cards, the
inventory filter, both policy editors and the seeded classification rules
at once — no code change, no image rebuild, which matters when the images
have to cross an air gap. In Helm it is `config.sites`, which lands in
the ConfigMap the API *and* every collector read, because a collector
derives each server's site at ingest and the two halves must agree. A
malformed value fails at startup rather than producing a site no server
could ever match. See `docs/adr/0018-sites-from-configuration.md`.

**Not every machine has a manager, and those are collected too.** The
`REDFISH_STANDALONE` collector reaches a BMC directly over DMTF Redfish —
any conformant one, including a Cisco server whose CIMC works but which
Intersight cannot yet manage. It is the one collector whose configuration
differs, and it differs because it has to: there is no aggregator to ask
what exists, so **the fleet comes from an inventory file the platform
owns**, and each BMC may have its own login. That file is also the only
collection filter, since a BMC does not know the server's `ocp4-...`
name. Two consequences worth knowing before deploying it: its cost is per
*server* rather than per manager, and a run where some hosts do not
answer is the normal outcome rather than a failure. See
`docs/adr/0016-redfish-standalone-collector.md` and
`docs/test-redfish-standalone-collector.md`.

Which collector produced a record is carried by `source_provider`, not by
the vendor — a Dell reached at its own BMC is still `vendor: dell`. So
`?source_provider=REDFISH_STANDALONE` answers "these have no manager,
don't look for them in OpenManage or UCS", and
`?vendor=cisco&source_provider=REDFISH_STANDALONE` answers "Cisco
standalone".

```
 UCS Central CronJob ─┐   (one login per UCS Manager domain)
 Redfish CronJob ─────┼── (one login per BMC, from an inventory file)
 OpenManage CronJob ──┼──▶  ProviderServer  ──▶  IngestService  ──▶  MongoDB
 Intersight CronJob ──┤       (vendor-neutral)   (classify, health,        │
 OneView CronJob ─────┘                           audit, upsert)          │
                                                                            ▼
                                                       FastAPI REST API (reads MongoDB,
                                                       Redis cache-aside on top)
                                                                            │
                                                                            ▼
                                                       React admin UI (inventory table,
                                                       server detail, classification/
                                                       health-policy editors)
```

MongoDB is the only thing that ties a collector run to what the UI shows
— a collector never talks to the API, and the API never talks to a
vendor manager directly. Adding a new vendor is: write a `ServerInventoryProvider`
implementation for it (see `app.infrastructure.providers.ucs_manager` as
the reference), register it in `tools/run_collector.py`, and add a
CronJob manifest — nothing in the API, the classification engine, the
health engine, or the frontend needs to change. `docs/adr/0009-ucs-
manager-collector.md` is the detailed writeup of how the first provider
was built and validated, and `docs/adr/0014-ucs-central-multi-domain-
collector.md` of how the Cisco collector drives it once per domain.

Three collectors exist today: `UCS_CENTRAL`, `INTERSIGHT` and
`REDFISH_STANDALONE`.

**`INTERSIGHT` is the only one that reaches this platform's 10,000-server
target without qualification.** Every child object in Intersight's model
carries a reference back to its owner, so each sub-resource is listed
once for the whole estate and joined in memory — one run costs on the
order of a hundred requests whether the tenant holds fifty servers or ten
thousand, against the Redfish collector's ~25 *per BMC*. It excludes
servers reporting `ManagementMode == UCSM` by default, because those are
exactly the ones `UCS_CENTRAL` already collects; the two partition the
Cisco fleet rather than fighting over it. **It has never been run against
a live Intersight** — the DevNet sandbox is offline until ~2027 — so read
`docs/adr/0017-intersight-collector.md` and follow
`docs/test-intersight-collector.md` before trusting it in production.
An air-gapped site needs an on-prem Private Virtual Appliance; it cannot
reach `intersight.com`.

`UCS_CENTRAL` covers the UCS-managed Cisco fleet. It asks UCS Central which domains are registered and what
their addresses are, then reads each domain's inventory live from that
domain's own UCS Manager — the data path validated end to end against a
live Cisco UCS Platform Emulator (see `docs/adr/0009`'s validation
sections). Central supplies the domain list and the service-profile
names; the servers themselves come from the domains. `docs/adr/0014`
covers the design, its costs, and what is still unproven — including that
a domain not registered with Central cannot be collected at all.

`OPENMANAGE` and `ONEVIEW` have configuration slots but no provider —
`tools/run_collector.py` raises a clear `NotImplementedError` for them
rather than silently doing nothing.

### Running the collector by hand

It needs five values: where UCS Central is and how to log in, plus a UCS
Manager login that works on every domain. There is deliberately no UCS
Manager endpoint — Central reports each domain's address itself. Set them
in `.env` (see `.env.example`) or inline:

```bash
export INVENTORY_UCS_CENTRAL_IP=ucsc.example.com   # bare host, never a URL
export INVENTORY_UCS_CENTRAL_USERNAME=inventory-svc
export INVENTORY_UCS_CENTRAL_PASSWORD=...
export INVENTORY_UCS_MANAGER_USERNAME=inventory-svc  # used on every domain
export INVENTORY_UCS_MANAGER_PASSWORD=...

# See what the fleet reports, without writing anything at all:
uv run python -m tools.run_collector --manager-type UCS_CENTRAL --dry-run

# ...one server only, plus every XML request/response on the wire:
uv run python -m tools.run_collector --manager-type UCS_CENTRAL \
  --dry-run --limit 1 --debug-xml

# The real thing — classify, health-evaluate, audit and upsert:
uv run python -m tools.run_collector --manager-type UCS_CENTRAL
```

A vendor with any of its required values missing is rejected as a
configuration error naming exactly what to set, rather than attempted as
a login that fails as "bad credentials".

`--dry-run` prints the `ProviderServer` each manager reports *before* the
ingestion pipeline reshapes it, including which site each name resolves
to — so a naming problem is visible without a write. `--debug-xml` turns
on `ucsmsdk`'s own request/response dump (passwords are masked by the
SDK); it is very verbose, so pair it with `--limit`.

Intersight takes an API key rather than a login, and has its own
read-only pre-flight:

```bash
export INVENTORY_INTERSIGHT_IP=intersight.com          # or an appliance FQDN
export INVENTORY_INTERSIGHT_API_KEY_ID='<API Key ID>'   # there is no username
export INVENTORY_INTERSIGHT_API_KEY_PEM="$(cat ~/intersight-key.pem)"

# Signs one GET, reports what the tenant holds, and checks TotalMemory
# against the sum of a real server's DIMMs. Writes nothing:
uv run python -m tools.verify_intersight

uv run python -m tools.run_collector --manager-type INTERSIGHT --dry-run
uv run python -m tools.run_collector --manager-type INTERSIGHT
```

Run the verifier first. This collector has never been run against a live
Intersight, and it settles the one assumption that would otherwise
silently mis-report every server's memory.
`docs/field-test-checklist.md` is the short version — the four variables
and the one command, plus what to send back; and
`docs/test-intersight-collector.md` is the full runbook.

## Local development

Requires [uv](https://docs.astral.sh/uv/), Node 24+, and a container
runtime (Podman or Docker) for the dev Mongo/Redis stack.

```bash
# 1. Bring up MongoDB + Redis (rootless podman/docker, no compose
#    provider required):
scripts/dev-up.sh up

# 2. Backend
uv sync --all-groups
cp .env.example .env          # adjust if needed
uv run uvicorn app.main:app --reload --port 8080 --app-dir backend

# 3. Seed realistic fake data (through the real ingestion pipeline —
#    not a shortcut that writes documents directly):
uv run python -m tools.seed_inventory --count 1000 --seed 42

# 4. Frontend
cd frontend && npm install && npm run dev
```

Then open http://localhost:5173 for the inventory UI, or
http://localhost:8080/docs for the API's OpenAPI docs.

When you are done, stop the UI and API processes and bring the stack
down with `scripts/dev-up.sh down` — a leftover container is the usual
cause of a later `pytest` run hanging.

### Fake data

`tools/seed_inventory.py` is the only way to get a fleet without vendor
hardware. It runs the *real* ingestion pipeline — the same
`ProviderServer` -> classify -> health-evaluate -> audit -> upsert path a
collector drives — so seeded data exercises what production does rather
than a shortcut that writes documents directly.

```bash
uv run python -m tools.seed_inventory --count 1000 --seed 42
```

`--count` defaults to 1000 and `--seed` to 42; the same pair always
produces the same fleet, field for field.

What you get mirrors the three collectors that exist. Cisco blades
arrive as `source_provider=UCS_CENTRAL` with Central-rooted DNs,
service-profile org paths and fabric attachments; Cisco rack units arrive
as `INTERSIGHT` with `intersight/<moid>` ids, no org path, and GPUs whose
identity is real but whose telemetry is `None` — the same ceiling the
real API has; and everything else — Dell, HPE, and `standalone`
whiteboxes — arrives as `REDFISH_STANDALONE` with `redfish://` addresses
and fully-populated GPUs. All three filters in the UI therefore have real
data behind them, and each collector's *absences* are reproduced too,
because a fixture richer than the real thing hides the gaps worth
seeing. Names span the estate's real shapes, including a
deliberate minority carrying no site token, so "Unassigned" is reachable.

**Re-seeding needs an empty database.** Servers correlate on
`(vendor, serial)`, so seeding a different `--count`/`--seed` (or a fleet
generated before a change to the generator) over an existing one reports
errors rather than replacing it. Wipe first:

```bash
scripts/dev-up.sh down && scripts/dev-up.sh up
uv run python -m tools.seed_inventory --count 1000 --seed 42
```

### Tests

```bash
uv run pytest                     # backend: unit + integration + api tests
uv run ruff check .               # lint
uv run ruff format --check .      # formatting
uv run mypy backend/app tools     # type check

cd frontend
npm run lint && npm run typecheck && npm run test -- --run && npm run build
npm run test:e2e                  # Playwright — needs the dev stack + backend + frontend all running
```

At scale, `tools/verify_indexes.py` and `tools/loadtest.py` verify query
plans and latency against a real 10k/50k-server seeded dataset — see
`docs/adr/0007-scale-verification-and-request-coalescing.md`.

## Container images

Every push to `main` that passes the full test suite builds and
publishes both images (API/collectors, and the frontend) to GHCR —
`ghcr.io/team-redbull/server_scan-api` and
`ghcr.io/team-redbull/server_scan-frontend` — tagged with a semantic
version decided automatically from
[Conventional Commits](https://www.conventionalcommits.org/) since the
last release (`feat:` → minor, `feat!:`/a `BREAKING CHANGE:` footer →
major, anything else → patch), plus rolling `latest` and `sha-<commit>`
tags. Note the patch number advances once per *commit*, not once per
push, so a push of three `fix:` commits moves it by three. See
`docs/adr/0010-image-publishing-and-versioning.md` for the full design
and `deploy/` for the manifests that consume these images.

Every action in the workflow is pinned to a commit SHA rather than a
tag, and none of it updates itself — `docs/adr/0013` explains why, and
`CLAUDE.md`'s "Keeping CI current" is the periodic pass that keeps it
from going stale.

## Project layout

```
backend/app/     FastAPI service — domain/application/infrastructure/api layers
frontend/        Vite + React + TypeScript SPA
tests/           unit / integration / api tests
tools/           operational CLIs: fake-data seeder, index/load verification,
                 the real-collector runner (tools/run_collector.py)
scripts/         dev environment helpers
deploy/          Helm chart (API, and per-vendor collector CronJobs)
docs/            architecture notes, ADRs, cisco-collectors.md (the
                 verified implementation facts the Cisco collectors rest on)
                 and test-ucs-collector.md (runbook for proving the
                 collector against real hardware)
```
