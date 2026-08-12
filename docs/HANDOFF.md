# Session handoff — resume slice 1

Written 2026-08-12, paused mid-slice-1 at the user's request, and pushed to
`main` as a WIP commit so the user can continue from git directly (possibly
in a different session/machine) rather than from this conversation. This is
the single source of truth for picking this back up: what's actually done
and verified vs. what two background agents were asked to build and may or
may not have finished as of the push.

**Snapshot at push time**: `uv run pytest tests/unit -q` → 49 passed.
`uv run ruff check .` → all checks passed. `backend/app/api/v1/servers.py`
and `schemas.py` now exist (the routes are wired into `main.py`) — the
backend agent (`a56ac87eb601c8e49`) was still running when this was pushed,
so **integration/api tests were not run** and the backend should be treated
as incomplete/unverified beyond the unit-test layer. The frontend agent
(`a9768ba30279dada7`) had already finished with all checks green (see
below) but only against mocked responses, not this backend.

## State of the repo

**Committed and pushed** (`main`, commit `f508120`): slice 0, the full
project skeleton — backend config/errors/logging/Mongo/Redis lifecycle,
health probes, metrics, container images, OpenShift/Helm manifests, CI,
frontend Vite scaffold. All tests passing at that commit. See
`docs/architecture.md` and `docs/adr/`.

**Uncommitted, on disk, NOT pushed** — everything below. Run `git status`
first thing on resume; this list was accurate as of pause time but the two
background agents (see next section) may still have been writing files.

- Domain layer (written and unit-tested by me directly, not delegated —
  treat this as solid): `backend/app/domain/enums/`,
  `backend/app/domain/value_objects/` (`normalize_mac`, `parse_bmc_address`),
  `backend/app/domain/models/` (`Server`, `Site`, `Manager`, `Hardware`,
  `NetworkInfo`, `Connectivity`, `Classification`, `Health`, `Maintenance`,
  `OpenShiftLifecycle`), `backend/app/domain/services/normalize.py` +
  `search_tokens.py`, `backend/app/domain/ports/` (`ServerRepository`
  Protocol, `ServerInventoryProvider`/`ProviderServer` Protocol — **these
  two port files are the fixed contract both background agents were told
  to build against; read them before touching anything downstream of
  them**). Tests: `tests/unit/domain/` (36 tests, last known-passing).
- `backend/app/errors.py` / `exception_handlers.py`: extended with the
  search/pagination `ErrorCode`s and `AppError` subclasses
  (`UnknownFilterError`, `UnknownSortFieldError`,
  `SearchQueryTooShortError`, `SearchQueryTooLongError`,
  `PageSizeTooLargeError`, `CursorInvalidError`,
  `CursorFilterMismatchError`) — this was in-progress from the backend
  agent, shown in a system reminder mid-session; looked complete and
  correct at last sight, but **re-run `uv run pytest tests/unit -q` and
  `uv run mypy backend/app` before trusting it**.
- `backend/app/config/settings.py`: gained `cursor_secret` (env
  `INVENTORY_CURSOR_SECRET`); `.env` / `.env.example` updated to match.

## Two background agents were dispatched and may still be running or may have finished

Check with the `ListAgents` tool first — agent processes may or may not
have survived the pause. If they're gone, their partial file output is
still on disk (see `git status`); read it, run the test suite, and decide
whether to finish by hand or re-dispatch.

**Agent `a9768ba30279dada7` (frontend) — FINISHED, reported all green**
before the pause: `npm run typecheck`, `npm run lint` (oxlint type-aware),
`npm run test -- --run` (4 files / 18 tests), `npm run build` all passed.
**Still needs**: a real run against the live backend (it was built and
tested entirely against mocked `fetch`, per instructions, since the
backend agent hadn't finished yet — integration-test it for real per step
5 below before trusting it end-to-end). Files: `frontend/src/types/server.ts`,
`frontend/src/api/servers.ts` + `queryKeys.ts`,
`frontend/src/lib/useDebouncedValue.ts`,
`frontend/src/components/{HealthBadge,LinkStateBadge,Badge}.tsx` (+ test),
`frontend/src/features/inventory/{hooks,InventoryTable,InventoryPage}.tsx`
(+ test), `frontend/src/features/servers/{hooks,OverviewTab,HardwareTab,
NetworkTab,ConnectivityTab,ServerDetailPage}.tsx` (+ ConnectivityTab test).
Modified `frontend/src/router.tsx`, `frontend/package.json` (added
`@tanstack/react-table` — landed at **v9**, a new major with a different
core API; the agent used the `/legacy` compat entry point
(`useLegacyTable`/`legacyCreateColumnHelper`) to get the familiar v8-shaped
manual server-side sort/pagination API — noted as a real decision worth a
second look, not a defect). Other flagged decisions: `site_id` filter is
free text (no sites-list endpoint exists yet to back a dropdown);
sortable columns limited to `name`/`model`/`updated_at` (only the ones
actually rendered, even though the backend contract also allows `serial`/
`last_seen_at`); "Previous" pagination is a bonus beyond spec, backed by a
locally-tracked cursor stack that won't survive a page reload (the
required forward "Next" flow is fully URL-backed and doesn't have this
limitation); bundle is ~453KB/134KB gzip, larger than a plain v8 setup
would likely be, probably from the legacy compat layer — not a blocker,
worth knowing.

**Agent `a56ac87eb601c8e49`** — backend: STILL RUNNING as of pause time (14+
min in). Mongo repositories
(`server_repository.py`, `site_repository.py`, `manager_repository.py`,
`indexes.py`), search/filter/sort/cursor domain logic
(`app/domain/services/search.py`, `cursor.py` — not yet confirmed written),
Redis `CacheClient` (`infrastructure/redis/cache.py`, `keys.py`), the fake
data generator + `IngestService` + `tools/seed_inventory.py`
(`infrastructure/providers/fake/`, `application/services/ingest.py`), and
the `GET /api/v1/servers` + `GET /api/v1/servers/{id}` routes
(`api/v1/servers.py`, `api/v1/schemas.py` — not yet confirmed written).
Full task spec (exact schemas, index list, error codes, test coverage
required) is in this conversation's transcript; if resuming in a new
session without that context, re-derive requirements from
`backend/app/domain/ports/repository.py` and `provider.py` (the contracts)
plus the "What to build" section pattern — sections 1–5 covered: search
service, cursor codec, Mongo repos + indexes, Redis cache, fake
provider/ingest/seed script, API routes.

**Agent `a9768ba30279dada7`** — frontend: `frontend/src/types/` (hand-written
TS types matching the API JSON shapes below), `frontend/src/api/servers.ts`
+ `queryKeys.ts`, `frontend/src/features/inventory/` (table + filters +
cursor pagination, URL-backed via `useSearchParams`),
`frontend/src/features/servers/ServerDetailPage.tsx` (Overview/Hardware/
Network/**Connectivity**/tabs — Connectivity tab must render a variable
number of fabric groups, never hardcode exactly A/B), `frontend/src/lib/useDebouncedValue.ts`,
`frontend/src/components/` (shared UI bits, e.g. tabs). Router already
edited: `/` → `InventoryPage`, `/servers/:id` → `ServerDetailPage`, `/status`
→ old `StatusPage`.

### The API contract both agents were told to build to (needed if resuming without transcript access)

`GET /api/v1/servers` — query params `search, site_id, vendor, manager_id,
installation_type, health_overall, maintenance, sort (name|serial|model|
updated_at|last_seen_at), sort_desc, cursor, page_size (max 200), with_count`.
Response:
```json
{"items": [{"id":"srv_...","name":"...","vendor":"dell","model":"...",
  "site_id":"...","manager_id":"...",
  "classification":{"installation_type":"HOSTED_CLUSTER"},
  "health":{"overall":"HEALTHY"},"maintenance":{"enabled":false},
  "connectivity":{"facts":{"fabric_paths_total":2,"fabric_paths_up":2,
    "fabric_paths_down":0,"fabrics_present":["A","B"]}},
  "last_seen_at":"...","updated_at":"..."}],
 "page":{"next_cursor":"...","has_more":true,"page_size":50,"count":null,"count_capped":false}}
```
List responses are a lean `ServerSummary` projection (no `hardware`
subdocument) — this was an explicit call driven by the user's stated
~10k-servers-with-headroom scale target; full detail only on
`GET /api/v1/servers/{id}`, which returns the complete `Server` shape
(identity, hardware, network, connectivity.attachments[], classification,
health, maintenance, tags, timestamps). Every error response is the RFC
9457 envelope already established in slice 0
(`{"type","title","status","detail","instance","code","request_id","details"}`),
parsed frontend-side by the existing `apiFetch`/`ApiError` in
`frontend/src/api/client.ts` — nothing new needed there.

## Exact steps to resume

1. `git status` — confirm what's actually on disk vs. this doc's snapshot.
2. `ListAgents` — see if either background agent is still running; if so,
   let it finish or send it a follow-up message rather than restarting.
3. Dev stack must be up: `scripts/dev-up.sh status`, and `scripts/dev-up.sh
   up` if not (rootless podman pod `server-inventory-dev`, Mongo on
   `:27017`, Redis on `:6379`).
4. Run everything: backend `uv run ruff check . && uv run ruff format
   --check . && uv run mypy backend/app && uv run pytest -q` (unit +
   integration + api); frontend `cd frontend && npm run lint && npm run
   typecheck && npm run test -- --run && npm run build`. Fix whatever's
   red — the agents were instructed to leave this green, but verify, don't
   trust.
5. **Do a real integration pass**, not just "both sides are green
   independently": run the seeder (`uv run python -m tools.seed_inventory
   --count 1000 --seed 42`) against the dev Mongo, start the backend
   (`uv run uvicorn app.main:app --reload --port 8080 --app-dir backend`),
   start the frontend (`cd frontend && npm run dev`), and actually click
   through: inventory table loads real seeded data, search `ocp-dell`
   narrows correctly, a Cisco server's detail page Connectivity tab shows
   real Fabric A/B state, pagination Next/back works, filters round-trip
   through the URL. This is the acceptance bar from the approved plan
   (`/home/toto/.claude/plans/claude-md-production-grade-bare-metal-lazy-petal.md`)
   — get here before considering slice 1 done.
6. Re-run `uv run mypy backend/app` and the frontend typecheck one more
   time after any manual fixes — both were strict-clean before the pause
   and must stay that way.
7. Commit and push as `TomerKarniol <tomer.karniol@gmail.com>`, **no
   co-author trailer**, one commit for the completed slice 1 with a clear
   message (matches the existing `f508120` commit's style/detail level).
8. Update `docs/architecture.md`'s "What's implemented vs. planned"
   section to move slice 1 from planned to implemented, and delete this
   file (`docs/HANDOFF.md`) once slice 1 is committed — it's a
   point-in-time resume note, not permanent documentation.

## What comes after slice 1

Per the approved plan: slice 2 (classification engine + rules API +
preview/reclassify) and slice 3 (health policy engine) can run in
parallel, both depending only on slice 1's `Server` model and repository
being in place. Slice 4 (maintenance + audit events) after those. Full
slice ordering and the classification/health algorithm designs (including
the `policy_key` shadowing mechanism for health policy overrides) are in
the approved plan file referenced above — re-read it before starting slice
2, don't re-derive the algorithms from scratch.
