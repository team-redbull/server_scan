# Server Inventory Platform

A production-grade, air-gapped bare-metal server inventory platform:
MongoDB-backed inventory, REST API, React UI, Redis cache, a regex-based
classification engine, and a declarative health-policy engine — built to
later accept real collectors for Dell OpenManage Enterprise, Cisco UCS
Manager, Cisco Intersight, and HPE OneView without a redesign.

## Status

Phase 1, slice 0 (project skeleton) — see `docs/` for architecture notes as
they land.

## Local development

Requires [uv](https://docs.astral.sh/uv/) and Node 24+.

```bash
# 1. Bring up MongoDB + Redis (works with plain rootless podman, no
#    compose provider required):
scripts/dev-up.sh

# 2. Install backend dependencies and run the API:
uv sync --all-groups
cp .env.example .env   # adjust if needed
uv run uvicorn app.main:app --reload --port 8080 --app-dir backend

# 3. Frontend (once scaffolded):
cd frontend && npm install && npm run dev
```

Alternatively, `compose.yaml` brings up the full stack (Mongo, Redis, API,
web) with `docker compose up` or `podman compose up` if a compose provider
is installed.

### Tests

```bash
uv run pytest              # unit + api tests
uv run ruff check .        # lint
uv run ruff format --check .
uv run mypy backend/app    # type check
```

## Project layout

```
backend/app/     FastAPI service — domain/application/infrastructure/api layers
frontend/        Vite + React + TypeScript SPA
tests/           unit / integration / api tests
tools/           operational scripts (fake data seeder, etc.)
scripts/         dev environment helpers
docs/            architecture notes and ADRs
```
