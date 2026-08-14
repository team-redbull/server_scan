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

See `docs/architecture.md` for the full per-slice writeup and
`docs/adr/` for the individual design decisions.

## How data actually gets in

This is the one idea worth understanding before anything else: **there is
no single "sync" process.** Each hardware vendor's manager (Cisco UCS
Manager, and eventually Dell OpenManage Enterprise, Cisco Intersight, HPE
OneView) gets its own small collector program, and each collector runs as
its own **Kubernetes `CronJob`** — one CronJob per manager *type*, not per
physical manager. On a schedule, a CronJob's pod:

1. Reads that vendor's endpoint and login from configuration — one set
   per manager type (`INVENTORY_UCS_MANAGER_IP` / `_USERNAME` /
   `_PASSWORD`, and the same shape for OneView, OME, UCS Central and
   Intersight). No `Manager` document to create, no secret volume to
   mount; in Kubernetes these arrive from a `Secret` via `envFrom`.
2. Talks to that vendor's real API and normalizes what it reports into
   the platform's vendor-neutral `ProviderServer` shape
   (`app.domain.ports.provider`).
3. Feeds that through the exact same ingestion pipeline every part of the
   platform already exercises with fake data
   (`app.application.services.ingest.IngestService`) — classify, health-
   evaluate, audit, and upsert into MongoDB, all in one write per server.

A server's **site** is not configured anywhere: it is parsed from the
server's own name (`ocp4-prod-one-infra-01` -> site `one`), so a
misconfigured manager cannot mislabel everything it collects. A name with
no site token is surfaced as "Unassigned" rather than defaulted.

```
 UCS Manager CronJob ─┐
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
manager-collector.md` is the detailed writeup of how the first one
(UCS Manager) was actually built.

Right now, only UCS Manager has a real collector, and it has been
validated end to end against a live Cisco UCS Platform Emulator (see
`docs/adr/0009`'s validation sections). The other manager types have
configuration slots but no provider — `tools/run_collector.py` raises a
clear `NotImplementedError` for them rather than silently doing nothing.

### Running a collector by hand

A collector needs three environment variables — where the manager is, and
a login. Set them in `.env` (see `.env.example`) or inline:

```bash
export INVENTORY_UCS_MANAGER_IP=ucsm.example.com   # bare host, never a URL
export INVENTORY_UCS_MANAGER_USERNAME=inventory-svc
export INVENTORY_UCS_MANAGER_PASSWORD=...

# See what a manager reports, without writing anything at all:
uv run python -m tools.run_collector --manager-type UCS_MANAGER --dry-run

# ...one server only, plus every XML request/response on the wire:
uv run python -m tools.run_collector --manager-type UCS_MANAGER \
  --dry-run --limit 1 --debug-xml

# The real thing — classify, health-evaluate, audit and upsert:
uv run python -m tools.run_collector --manager-type UCS_MANAGER
```

A vendor with any of its three values missing is rejected as a
configuration error naming exactly what to set, rather than attempted as
a login that fails as "bad credentials".

`--dry-run` prints the `ProviderServer` each manager reports *before* the
ingestion pipeline reshapes it, including which site each name resolves
to — so a naming problem is visible without a write. `--debug-xml` turns
on `ucsmsdk`'s own request/response dump (passwords are masked by the
SDK); it is very verbose, so pair it with `--limit`.

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
docs/            architecture notes and ADRs
```
