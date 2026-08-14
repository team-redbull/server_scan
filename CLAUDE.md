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

## Current status

Phase 1 slices 0–7 are done (see `docs/architecture.md`'s "What's
implemented vs. planned" section for the full per-slice writeup):
inventory + search/pagination + UI, classification engine, health policy
engine, maintenance + audit trail, classification/health admin UIs, a
10k/50k performance pass, and Playwright E2E coverage.

Beyond the numbered slices, the **first real vendor collector — Cisco UCS
Manager** — is built and pushed
(`docs/adr/0009-ucs-manager-collector.md`). This is the actual frontier
of the project right now: real data acquisition, not the API/UI shell
around it.

### The collector architecture (read this before touching a collector)

There is no single sync process. Each hardware vendor gets its own
`ServerInventoryProvider` implementation
(`app.infrastructure.providers.<vendor>`, following the seam
`app.domain.ports.provider` defines and `app.infrastructure.providers.
fake` — the Phase-1 synthetic-data provider — already exercises), and
each manager *type* gets its own Kubernetes `CronJob` running
`tools/run_collector.py --manager-type <TYPE>`. A run:

1. Looks up every enabled `Manager` document of that type from MongoDB.
2. Resolves real credentials from a mounted `Secret` via
   `app.domain.ports.credentials.CredentialResolver` (currently one
   implementation, `FilesystemCredentialResolver` — reads
   `{credentials_dir}/{credential_ref}/{username,password}`, matching
   how Kubernetes projects a Secret as a volume).
3. Talks to the vendor API, normalizes into `ProviderServer`.
4. Runs that through `app.application.services.ingest.IngestService` —
   the exact same pipeline the fake-data seeder and every other
   provider use: classify, health-evaluate, audit, upsert, one write per
   server.

A collector never talks to the FastAPI process; the API never talks to a
vendor manager. MongoDB is the only thing connecting them. See
`README.md`'s diagram and `docs/adr/0009-ucs-manager-collector.md` for
the concrete UCS Manager build (what's confirmed against real `ucsmsdk`
source vs. what's still an assumption pending a real domain/emulator
test).

**Only UCS Manager has a real collector.** `OPENMANAGE`, `INTERSIGHT`,
`ONEVIEW`, and `UCS_CENTRAL` are known `ManagerType` values with no
implementation — `tools/run_collector.py`'s `_PROVIDER_FACTORIES` raises
a clear `NotImplementedError` for them, not a silent no-op. Building the
next one means: implement `ServerInventoryProvider` for it under
`app.infrastructure.providers.<vendor>`, add it to
`_PROVIDER_FACTORIES`, add a CronJob manifest (mirror
`deploy/openshift/ucs-manager-collector-cronjob.yaml` and the Helm
template).

### What's explicitly NOT done yet (in rough priority order the user has confirmed)

1. **Dell OpenManage / Cisco Intersight / HPE OneView collectors.** Not
   started. Before picking one: research each vendor's *current* API
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
   update). No Kubernetes/OpenShift manifests exist for the frontend
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

For a real UCS Manager collector test without production hardware:
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

## Where to continue right now

The most recent user direction was: real vendor collectors first,
deployment/CD gaps and auth deliberately parked. The natural next steps,
in the order the user has been steering toward:

1. Validate the UCS Manager collector end-to-end against UCSPE or real
   hardware, and fix whatever the confirmed-vs-assumed gaps in
   `docs/adr/0009` turn out to be wrong about (CPU model, storage
   detail, fabric interconnect identity, the memory-unit assumption).
2. Build the next vendor collector. Ask the user which one before
   assuming — their last stated preference was "easiest to actually
   test," which favored UCS's real emulator; re-evaluate that tradeoff
   fresh for whichever vendor comes next rather than assuming the same
   research still holds.
3. Once collectors are further along (or if the user redirects), the
   deployment/CD and auth gaps above are the rest of what "production
   and really run" means for this platform.
