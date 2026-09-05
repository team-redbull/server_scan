# Audit — deployment, CI, and documentation (2026-09-06)

Scope: `.github/workflows/`, `Containerfile`, `frontend/Containerfile`,
`compose.yaml`, `scripts/`, `deploy/`, all of `docs/`, `README.md`,
`CLAUDE.md`, `pyproject.toml`, `requirements.txt`, `pylock.toml`,
`.env.example`. Read-only pass on branch `dev-refactor`; nothing was
changed. Backend, tools/tests and frontend source are other agents'
scope and are referenced here only where a doc claims something about
them.

---

## 1. CI map (`.github/workflows/ci.yml`)

Trigger: `push` on `branches: ["**"]` only — no `pull_request`, by a
documented decision (lines 7–14); fork PRs would get no CI.
`concurrency` cancels in-flight runs per ref except on `main` (18–20).
Top-level `permissions: contents: read`.

Four independent jobs run in parallel, then one gated publish:

| Job | Runner setup | Steps | Gates |
|---|---|---|---|
| `lint` (41–76) | checkout, setup-uv (cache on `uv.lock`), `uv sync --all-groups` | `ruff check .` → `ruff format --check .` → `ty check backend/app tools` — **three separate steps**, so a formatting-only failure is distinguishable and cannot be skipped | `publish` |
| `test` (78–123) | same + `mongo:8` and `redis:8-alpine` service containers with health checks | `pytest --cov=app --cov-report=term-missing --cov-report=xml`, then uploads `coverage.xml` | `publish` |
| `frontend` (125–154) | setup-node 24, npm cache on `frontend/package-lock.json`, `npm ci` | lint → typecheck → `test -- --run` → build | `publish` |
| `e2e` (156–252) | uv + node + both services; `playwright install chromium --only-shell` | starts uvicorn on 8080 and polls `/health/ready`, seeds 300 servers, starts Vite on 5173, `npx playwright test`; uploads the report `if: failure()` | `publish` |
| `publish` (254–465) | `needs: [lint, test, frontend, e2e]`, `if: github.ref == 'refs/heads/main'`, elevates to `contents: write` + `packages: write` | see below | — |

`publish`, in order: full-history checkout → `PaulHatch/semantic-version`
computes the next tag from Conventional Commits → a hand-written guard
verifies the tag is non-empty, does not already exist, and sorts newer
than the previous tag (295–310) → `git tag`/`git push` → an inline
Python script groups commit subjects since the previous *reachable* tag
into `### Breaking` / `New features` / `Fixed` / `Performance` /
`Documentation` (+ `Other`), dropping `refactor|test|chore|style|ci`,
and `gh release create` publishes them (334–403) → GHCR login → buildx →
metadata + build/push for the API image (`Containerfile`, context `.`)
and the frontend image (`frontend/Containerfile`, context `./frontend`),
each tagged `{{version}}`, `{{major}}.{{minor}}`, `{{major}}`, `latest`,
`sha-<commit>`, both `linux/amd64` only, with separate GHA cache scopes.

This matches ADR-0010 and CLAUDE.md convention 9 exactly. Two structural
problems are in the findings table below (C1: the tag and GitHub Release
are created *before* the images are built; C4: `uv sync` is not
`--locked`).

### Action pins (ADR-0013)

Every `uses:` was resolved against the live tag via `gh api`. **All nine
`# vX.Y.Z` comments match the SHA they annotate — no pin lies.** One
action is behind:

| Action | Pinned | Comment | Comment accurate? | Latest release |
|---|---|---|---|---|
| `actions/checkout` | `3d3c42e…` | v7.0.1 | yes | v7.0.1 |
| `astral-sh/setup-uv` | `20cfd1bf…` | v10.0.1 | yes | v10.0.1 |
| `actions/upload-artifact` | `043fb46d…` | v7.0.1 | yes | v7.0.1 |
| `actions/setup-node` | `82076278…` | v7.0.0 | yes | v7.0.0 |
| `PaulHatch/semantic-version` | `9f728303…` | v6.0.2 | yes | v6.0.2 |
| `docker/login-action` | `dbcb8138…` | v4.6.0 | yes | v4.6.0 |
| `docker/setup-buildx-action` | `bb05f3f5…` | v4.2.0 | yes | **v4.3.0** |
| `docker/metadata-action` | `dc802804…` | v6.2.0 | yes | v6.2.0 |
| `docker/build-push-action` | `53b7df96…` | v7.3.0 | yes | v7.3.0 |

### Caching, parallelism, wasted minutes

Caching is in place everywhere it matters (uv cache keyed on `uv.lock`,
npm cache keyed on the lockfile, per-image GHA buildx caches). The four
gate jobs are genuinely independent. The only duplicated work is `e2e`
re-installing the whole backend + frontend toolchain that `test` and
`frontend` already installed — unavoidable without artifacts, and both
are cache-warm, so this is not worth changing. `coverage.xml` is
uploaded and consumed by nothing (M2).

---

## 2. Deploy gap list

### `deploy/helm/server-inventory` renders and lints clean

`helm lint` passes (one INFO about a missing chart icon); `helm template`
with all five collectors enabled renders **20 resources**. Templates
present: backend ConfigMap / Deployment / Service / Route, the collector
credentials Secret, and one CronJob each for `ucs-central`, `intersight`,
`openmanage`, `oneview`, `redfish-standalone`.

### Confirmed gaps

| # | Gap | Detail |
|---|---|---|
| D1 | **`INVENTORY_CURSOR_SECRET` cannot be set through the chart at all** | Not in `values.yaml`, not in `backend-configmap.yaml`, not in the Deployment `env`. CLAUDE.md and `arc42.md:423` both understate this as "not enforced at startup" — it is worse: there is no supported way to override it short of editing a template. Every production install signs pagination cursors with the committed literal `dev-insecure-cursor-secret-change-in-production` (`settings.py:74`). See S1. |
| D2 | **No frontend manifests** | Verified: `templates/` has no frontend Deployment/Service/Route, and `values.yaml` has no `frontend:` block, despite `frontend/Containerfile` existing and CI publishing `…-frontend` on every push to main. Quantified: 3 new templates (Deployment, Service, Route) + a `frontend.image/replicaCount/resources` values block + a runtime API-base-URL knob ≈ 120 lines. `deploy/README.md:118–121` states this correctly. |
| D3 | **CORS is empty in the shipped values** | `config.corsAllowedOrigins: ""` → `_split_csv` yields `[]` → `CORSMiddleware(allow_origins=[])`. Once D2 is closed, the browser gets an opaque CORS failure with nothing in the API log pointing at it. |
| D4 | Inconsistent pod hardening | `seccompProfile: RuntimeDefault` and `automountServiceAccountToken: false` appear **only** on `redfish-standalone-collector-cronjob.yaml` (132–133, 52). The API Deployment and the other four CronJobs have neither. |
| D5 | No PodDisruptionBudget, no anti-affinity, no NetworkPolicy, no ServiceAccount | `backend.replicaCount: 2` with nothing stopping both replicas landing on one node, and no egress policy on collector pods that reach the whole BMC network. |
| D6 | `Chart.yaml` `version`/`appVersion` frozen at `0.1.0` | CI derives real image versions from Conventional Commits; the chart's own version never moves, so `helm history` cannot tell two installs apart. |
| D7 | `backend.image.tag: latest` | Documented as such in the comment, but it is the shipped default and defeats the whole point of the semver tags CI publishes. |
| D8 | No CD/GitOps wiring | Correctly documented in `deploy/README.md:123–126`; listed here for completeness. |

### What is correctly wired

- **`INVENTORY_SITES` reaches both halves**, exactly as CLAUDE.md claims:
  `backend-configmap.yaml:14` puts it in `<release>-api-config`, and all
  five CronJobs `envFrom` that same ConfigMap. Same for
  `INVENTORY_GPU_MODELS` and `INVENTORY_NIC_OS_NAMES`.
- Probes: liveness `/health/live` and readiness `/health/ready` on the
  API, both `httpGet` on the named port. No startup probe, which is fine
  at a 5s initial delay.
- Resource requests and memory limits on every workload; CPU limits
  deliberately absent (correct — CPU limits cause throttling).
- Credentials arrive via `envFrom: secretRef`, never inline `env`, with
  `existingSecret` as the production escape hatch — the reasoning is
  written down in the template header and it is right.
- `concurrencyPolicy: Forbid` and `activeDeadlineSeconds` on every
  CronJob, each set above its in-process run budget so the process
  reports a summary before the pod is killed.

### Containerfiles

`Containerfile` (backend) is in good shape: UBI9-minimal pinned to the
9.8 minor stream, a separate `deps` stage so the dependency layer caches
independently of application code, `microdnf clean all`, an explicit
non-root UID 1001, `PYTHONDONTWRITEBYTECODE`, a HEALTHCHECK, and a
comment telling an air-gapped builder to replace the `ADD` of the
python-build-standalone tarball with a `COPY` from a mirror. Two notes:
the `ADD` is unverified (no checksum — see M6), and there is no
`.dockerignore` at the repo root.

`frontend/Containerfile` is the weak one: **both stages use floating
`:latest` tags** (`ubi9/nodejs-22:latest`, `ubi9/nginx-124:latest`),
which contradicts the backend's own pinning rationale and ADR-0013, and
makes an air-gapped rebuild unreproducible. `deploy/air-gapped-images.txt`
lines 38–39 copy the `latest` through. `COPY . .` with no
`frontend/.dockerignore` sends `node_modules`, `dist` and `.git` into the
build context. See C3.

---

## 3. `.env.example` drift table

**Result: zero drift on the settings module.** All 70 fields on
`Settings` have a matching `INVENTORY_*` entry in `.env.example`, and all
70 documented entries are read. Verified programmatically (field names →
`INVENTORY_<UPPER>`, set-differenced both ways); both differences are
empty. This is unusually good and worth saying plainly.

The one real drift is an env var read **outside** `Settings`:

| Variable | Read at | In `.env.example`? | Note |
|---|---|---|---|
| `INVENTORY_UCS_DUMP_XML` | `backend/app/infrastructure/providers/ucs_manager/client.py:92`, `tools/verify_ucs_central.py:206` | **no** | Direct `os.environ.get`, which contradicts `settings.py`'s own docstring ("never scatter `os.environ` calls through the codebase") and `.env.example:2–3` ("do not read os.environ anywhere else"). It is the debug-XML switch `--debug-xml` documents, so the behaviour is intentional; the convention violation and the undocumented variable are not. |

`requirements.txt` and `pylock.toml` are **fresh**: every one of the 13
runtime pins in `pyproject.toml` appears at the identical version in
both, and the 7 dev-group pins are absent from both, which is exactly
what `--no-dev` produces. No drift.

---

## 4. False documentation statements

Ranked roughly by how much damage acting on them would do.

| Doc:line | Claims | Actually true |
|---|---|---|
| `docs/architecture.md:309–350` | "**Slice 5**: the classification-rule and health-policy **admin UIs** (`frontend/src/features/classification/`, `frontend/src/features/health/`)" — then 40 lines describing editable forms, a `ConditionBuilder`, a `ShadowPanel`, "every field disabled except `enabled`, matching that the backend only permits an enable/disable update to a `system: true` record". | Rules and policies are **read-only**, merged into one `frontend/src/features/rules/RulesPage.tsx` (commits `27b20a8`, `f9ab059`). `classification_rules.py` and `health_policies.py` expose **only `@router.get`** — no POST/PUT/DELETE, no preview endpoints. `PreviewPanel`/`ConditionBuilder`/`ShadowPanel` exist nowhere in `frontend/src`. This is the single most misleading section in the repo: ~40 lines describing a subsystem that was deleted. |
| `docs/arc42.md:436–440` | "The inventory page's **Source** filter … still offers only `UCS_CENTRAL`/`INTERSIGHT`/`REDFISH_STANDALONE` — `OPENMANAGE` and `ONEVIEW` servers cannot be filtered for." | `frontend/src/api/sites.ts:86–92` lists **all five**. Fixed; the risk register still reports it as open. |
| `README.md:350–352` | "The UI's Source filter has not caught up with `ONEVIEW` or `OPENMANAGE` either; it still offers three values." | Same — offers five. |
| `docs/architecture.md:306` | "334 backend tests (unit/integration/api) … pass." | `pytest --collect-only` reports **1037**. |
| `docs/diagrams/runtime-architecture.architecture.json` (+ the rendered `.html`) | View note: "Five read-only collectors, one CronJob per manager type." Component list: `ucscentral`, `intersight`, `bmcs` only. | **HPE OneView and Dell OpenManage appear nowhere in the diagram** — no component, no edge, no label (grep for "OneView\|OpenManage\|Dell\|HPE" in the HTML returns nothing). Pinned to revision `4c116b2`, before both collectors landed. |
| `docs/arc42.md:14` | "`docs/adr/` — 18 records" | 22. (The ADR index at `arc42.md:346–367` correctly lists all 22, so only the pointer table is stale.) |
| `docs/arc42.md:396` (Q6) | A site rename "Reaches API, UI, filters, **editors** and seeded rules." | There are no editors. |
| `docs/arc42.md:335–337` | "Note that `CLAUDE.md` describes this as '`AuthProvider`/RBAC scaffolding'; no such class exists, and this document is the accurate one." | CLAUDE.md convention 6 was since corrected and now says the same thing. arc42's correction-of-CLAUDE.md is itself now the stale statement. |
| `docs/arc42.md:423` | `INVENTORY_CURSOR_SECRET` risk: "Documented in a code comment; not enforced at startup." Filed **Medium**. | True as far as it goes, but incomplete and mis-severitied: the chart provides no way to set it at all (D1/S1). Belongs in High. |
| `docs/architecture.md:285` | "`AuditService.record()` is the one place every mutation (**classification rule CRUD, health policy CRUD**, maintenance changes, …) goes through." | Rule and policy CRUD no longer exists. |
| `README.md:26` | "5. Classification-rule and health-policy **admin UIs**." | Read-only, one page. Same error at `README.md:96` ("both policy editors") and `README.md:137–138` (the ASCII diagram's "classification/health-policy editors"). |
| `README.md:58` | "…and **eventually** Dell OpenManage Enterprise and HPE OneView…" | Both shipped (ADR-0020, ADR-0022). Contradicted by `README.md:152` eleven lines later. |
| `CLAUDE.md:295` | "Every `ManagerType` now has an entry in `tools/run_collector.py`'s `_PROVIDER_FACTORIES`" | The symbol is `PROVIDER_FACTORIES` — public, no leading underscore, and deliberately so (`run_collector.py:364–369` explains why it is public). CLAUDE.md itself uses the correct name later, at the "Give the Dell collector a seeded shape" item. The *substance* of the claim is correct: five entries, `UCS_MANAGER` absent by design. |
| `CLAUDE.md` ("supply-chain pass" paragraph) | "`pip-audit` and `npm audit` are both clean as of that commit." | `npm audit` is still clean (0 vulnerabilities). **`pip-audit` is not**: `cryptography==46.0.3` has **11 findings**. See S2. |
| `scripts/dev-up.sh:4–6` | "This machine has rootless podman 4.9 but no `podman-compose` and no `docker-compose` plugin installed, so `compose.yaml` … isn't runnable out of the box here" | CLAUDE.md's own measured table (2026-09-05) records Podman 6.1.1, Docker Compose v5.5.1 and podman-compose 1.6.0 all present, with `docker compose` the *preferred* path. The script's stated reason for existing is false; its actual value (no compose provider needed at all) is real. |
| `scripts/dev-up.sh:7–8` | "…per spec section 52 (\"`docker compose up` must be sufficient for development\")." | Cites the 75-section chat spec as justification, which CLAUDE.md convention 1 explicitly forbids ("never 'the spec says so'"). |
| `backend/app/api/v1/classification_rules.py:1`, `health_policies.py:1` | Module docstrings: "CRUD + preview". | Two GETs each. Flagged for the backend agent; noted here because it is the same stale-shape problem. |
| `docs/test-redfish-standalone-collector.md` | `curl -s 'localhost:8080/api/v1/servers?site_id=one'` | `one` is not a code in the shipped `INVENTORY_SITES` (`nyc,tlv,bat-yam,five`), so the example returns nothing. Cosmetic. |

### ADR set — structure

- **Numbering is clean**: 0001–0022, no gaps, no duplicates.
- **Supersessions are recorded in both directions where they exist**:
  ADR-0011 carries a "Partly superseded by" banner at line 3; ADR-0018
  states "Supersedes part of 0011"; ADR-0012 states "Partially
  superseded by ADR-0014's 2026-08-17 update"; ADR-0020 names what it
  supersedes in `docs/dell-collectors.md`. `docs/cisco-collectors.md:613`
  records the ADR-0021 reversal in place. **All the supersession claims
  CLAUDE.md makes check out.**
- **Front matter is inconsistent — five distinct shapes** (M1):
  `# ADR-000N: Title` + `## Status` section (0001–0013, 0015);
  adr-tools style `# 14. Title` + bare `Date:` + `## Status` (0014,
  0016, 0021); inline `**Status:** …` (0017, 0018, 0019); a bullet list
  `- Status:` / `- Date:` (0020); bare `Date:` / `Status:` lines (0022).
  ADRs 0001–0013 carry **no date at all**.
- **No decision found in the code without an ADR.** The read-only
  rules/policies change is the closest call — it is a real architectural
  reversal of slice 5 with no ADR — see M3.
- **`CHANGELOG.md`: no stale references.** The three hits
  (`CLAUDE.md:159`, `ci.yml:324`, `docs/notes/refactor-prompt.md:309`)
  all correctly describe it as deleted.

---

## 5. Findings, ranked

Severity: **security** > **correctness** > **maintainability** >
**consistency**. Effort S = under an hour, M = half a day, L = more.

### Security

| # | File:line | Finding | Fix | Effort |
|---|---|---|---|---|
| S1 | `deploy/helm/server-inventory/values.yaml` (absent), `backend-deployment.yaml:29–44`, `backend/app/config/settings.py:74` | **Every production install of this chart signs pagination cursors with the committed literal `dev-insecure-cursor-secret-change-in-production`.** The chart exposes no way to set `INVENTORY_CURSOR_SECRET`, and nothing validates it at startup. A cursor is HMAC-signed to stop a client forging a position or unbinding it from its filter/sort (`domain/services/cursor.py:6–17`); with a publicly-readable key that protection is nil. | Two parts, both small: (a) add `INVENTORY_CURSOR_SECRET` to the Deployment `env` via `secretKeyRef` on `db.secretName` (or a new `api.secretName`), documenting the key in `deploy/README.md`; (b) fail fast in `create_app` when `environment == "production"` and `cursor_secret` is the default. | S |
| S2 | `pyproject.toml:27` | `cryptography==46.0.3` carries **11 known vulnerabilities** (PYSEC-2026-35/36/2141/3552/3553/3554, GHSA-537c-gmf6-5ccf); fixes land across 46.0.5 → 50.0.0. It is a *direct* runtime dependency (Intersight request signing), so it ships in the API image and every collector pod. CLAUDE.md still asserts pip-audit is clean. | Bump toward 50.0.1, regenerate `requirements.txt` + `pylock.toml` and `uv.lock`, re-run `pip-audit`, and correct the CLAUDE.md sentence. Check the air-gapped mirror carries the target version first (the `redis==8.0.1` and `ucsmsdk` comments show this constraint is real here). | S |
| S3 | `.github/workflows/ci.yml` (absent) | **No `pip-audit` or `npm audit` step anywhere in CI.** ADR-0013 deliberately removed Dependabot and made currency a manual quarterly chore — with no automated signal, S2 is exactly the failure mode that produces. | Add a non-blocking `security` job running `uv run --with pip-audit pip-audit --skip-editable` and `npm audit` in `frontend/`. Non-blocking on purpose: a new advisory must not red-line an unrelated PR, but it must be visible. | S |
| S4 | `deploy/.../redfish-standalone-collector-cronjob.yaml:52,132–133` vs. the other five workloads | `automountServiceAccountToken: false` and `seccompProfile: RuntimeDefault` are set on exactly one of six workloads. The other four CronJobs and the API Deployment mount a service-account token none of them use. | Lift both into every pod spec. Under OpenShift `restricted-v2` the seccomp profile is defaulted anyway, but the token mount is not. | S |
| S5 | `frontend/Containerfile:8,18`; `deploy/air-gapped-images.txt:38–39` | Both frontend base images use floating `:latest`. An air-gapped rebuild is not reproducible, and a compromised or simply changed upstream tag lands silently — the exact threat the backend `Containerfile:11–16` and ADR-0013 argue against. | Pin both to a minor stream the way the backend pins `9.8`, and update `air-gapped-images.txt`. | S |

### Correctness

| # | File:line | Finding | Fix | Effort |
|---|---|---|---|---|
| C1 | `.github/workflows/ci.yml:312–320` vs `431–465` | **The git tag and the GitHub Release are created before the images are built.** If either `build-push` step fails, `vX.Y.Z` and its release notes exist with no images behind them — and the guard at 295–310 then *refuses* to re-tag, so a re-run cannot recover; the next merge simply skips that version. | Move "Tag the release" and "Publish the release notes" after both build/push steps. The version is already computed early, so nothing else needs reordering. | S |
| C2 | `scripts/dev-up.sh:17,22–27,34–37,74,82–83` | `CONTAINER_RUNTIME=docker` is offered in the error message at line 23 but **cannot work**: `pod exists`, `pod create`, `pod rm` and `pod ps` are Podman-only subcommands. | Either drop the docker suggestion from line 23 and rename the variable, or branch to `docker network create` + two `docker run`s. Given `compose.yaml` already covers docker, deleting the false promise is the smaller fix. | S |
| C3 | `frontend/Containerfile:15`; no `frontend/.dockerignore`, no root `.dockerignore` | `COPY . .` in the build stage sends the whole frontend directory — a local `node_modules`, `dist`, `playwright-report` and `.git` — into the build context, then over-writes the `npm ci` result. Slow, and it can smuggle host artifacts into the image layer cache. | Add `frontend/.dockerignore` with `node_modules`, `dist`, `playwright-report`, `test-results`, `.git`. | S |
| C4 | `.github/workflows/ci.yml:57,114,192` | `uv sync --all-groups` without `--locked`/`--frozen`: a `pyproject.toml` change with a stale `uv.lock` re-resolves silently in CI and passes. Given ADR-0013's and ADR-0019's whole argument is that nothing may move underneath the gate, this is a hole in it. | `uv sync --all-groups --locked` in all three jobs. | S |
| C5 | `requirements.txt`, `pylock.toml` (no guard) | Both are correct **today**, but nothing checks them. CLAUDE.md makes regenerating them a manual obligation after any dependency change; the air-gapped install path (`docs/air-gap.md:41–51`) depends entirely on them being current. | One CI step: re-run both `uv export` commands into a temp dir and `diff` against the committed files. | S |

### Maintainability

| # | File:line | Finding | Fix | Effort |
|---|---|---|---|---|
| M1 | `docs/adr/*.md` (headers) | Five different front-matter shapes across 22 ADRs; 0001–0013 have no date. Nothing can parse the set — status, date and supersession all have to be read by eye. | Normalise on one header (title / Date / Status / Supersedes-Superseded-by), backfilling dates from `git log --diff-filter=A`. Do not rewrite bodies. | M |
| M2 | `.github/workflows/ci.yml:119–123` | `coverage.xml` is uploaded on every run and read by nothing — no threshold, no reporter, no trend. | Either add `--cov-fail-under=<current>` to make it a gate, or drop the upload. | S |
| M3 | `docs/adr/` (absent) | Making rules and policies read-only and removing their write endpoints is a real architectural reversal of slice 5, with a `feat!:` breaking commit behind it and no ADR. It is currently only recoverable from `git log`. | ADR-0023: "Classification rules and health policies ship with the platform and are read-only", recording that every deployment must classify and score identically. Then rewrite `architecture.md`'s slice 5 to point at it. | M |
| M4 | `deploy/helm/server-inventory/Chart.yaml:5–6` | Chart `version`/`appVersion` pinned at `0.1.0` forever while images are versioned automatically. | Have the publish job `sed` `appVersion` to the computed tag, or accept it and say so in a comment. | S |
| M5 | `values.yaml:7–8` | `backend.image.tag: latest`. | Default to a real release tag, or leave `latest` and add a `helm upgrade` note. Documented already; listed for the deploy checklist. | S |
| M6 | `Containerfile:25` | The python-build-standalone tarball is `ADD`ed over HTTPS with no checksum verification. The comment tells an air-gapped builder to swap in a `COPY`, but a connected build (which is what CI runs) trusts the download. | `ADD --checksum=sha256:…` (BuildKit supports it) alongside the existing `ARG`s. | S |
| M7 | `docs/arc42.md:4` | "Written 2026-08-30 against commit `e570fa8`" — the section header at line 373 says the risk register is "the one most likely to go stale — treat its date as load-bearing", and it has. Four of its entries are now wrong or mis-severitied. | Re-date and rework §11 as part of fixing the false statements in §4 above. | M |

### Consistency

| # | File:line | Finding | Fix | Effort |
|---|---|---|---|---|
| K1 | `README.md:304` vs `scripts/dev-up.sh:67` | Two different documented ways to start the API (`--app-dir backend` from the root vs `cd backend && …`). Both work; the divergence is noise. | Use the README form in both. | S |
| K2 | `values.yaml` — `redfishStandalone.suspend: true` only | The two collectors with the loudest "NEVER VALIDATED AGAINST LIVE HARDWARE" banners (`oneview`, `intersight`) have no `suspend` field; only the already-validated Redfish one does. | Add `suspend` to the oneview and intersight CronJobs, defaulted `true`, so enabling is a two-step act. | S |
| K3 | `Chart.yaml` | No `icon`, no `maintainers`, no `home` (helm lint INFO). | Cosmetic; fix while touching M4. | S |

---

## 6. What is already good

Said plainly, because this repo does several things better than most
production codebases I have audited:

- **`.env.example` has zero drift from `Settings`.** 70 fields, 70
  documented entries, in both directions. The task brief called this "a
  common silent drift" — it is not present here. The `gpu_models`
  comment in `settings.py:92–110` even records a past instance of
  exactly this bug and how it was caught, which is why it is not
  recurring.
- **Every CI action pin's version comment is accurate.** All nine
  verified against the live tag. One action is a single minor behind.
  ADR-0013's manual-maintenance obligation is actually being met.
- **`requirements.txt` and `pylock.toml` are in sync with
  `pyproject.toml` right now** — every runtime pin matches, every dev
  pin correctly absent.
- **The ADR discipline is genuinely strong.** 22 records, no gaps, no
  duplicates, supersessions recorded in *both* the superseding and the
  superseded document, and reversals of earlier reasoning marked in
  place at the point where the old reasoning lives
  (`cisco-collectors.md:613`). Every supersession claim CLAUDE.md makes
  about the ADR set checks out.
- **The lint job's three separate steps** do exactly what CLAUDE.md says
  they do, and the split means a formatting-only failure is legible.
- **The release-notes generator is the right design.** Deriving notes
  from the same commit subjects that decide the version makes drift
  structurally impossible, and the `refactor|test|chore|style|ci` drop
  list is the correct call.
- **The version guard at `ci.yml:295–310`** — refusing an empty tag, an
  existing tag, or a backwards tag — is the kind of check most pipelines
  learn to add only after a bad release.
- **`docs/notes/` as a separate tier** keeps vendor-API research out of
  the ADR set without losing it.
- **The Helm chart's comments carry the reasoning, not just the value.**
  `count=-1` meaning 64, `activeDeadlineSeconds` deliberately above the
  in-process budget so a run can report what it collected, `backoffLimit: 1`
  weighing a lagging ConfigMap mount against credential spray — these
  are decisions written down where the person changing them will read
  them.
- **`envFrom: secretRef` over inline `env`**, with the `kubectl describe`
  reasoning stated in the template header, and `existingSecret` as a
  first-class production path.
- **`.gitignore`** correctly protects Redfish inventories and credential
  files with a comment explaining that an inventory alone discloses
  estate topology.
- **`npm audit` is clean** (0 vulnerabilities).
- **`helm lint` passes and the chart renders 20 resources** with every
  collector enabled — no broken template paths.

---

## 7. Questions for the user

1. **`INVENTORY_CURSOR_SECRET` (S1)** — do you want it wired into
   `db.secretName` (fewest moving parts, but mixes an app secret into
   the DB secret), or a separate `api.secretName`? And should the API
   *refuse to start* in `environment == production` with the default, or
   only log an error? Refusing is the honest option but turns a silent
   weakness into a hard deploy failure for anyone upgrading.
2. **`cryptography` (S2)** — does the air-gapped mirror carry 50.x? The
   `redis==8.0.1` and `ucsmsdk==0.9.18` comments say pins here track the
   mirror, not PyPI, so the target version is your call, not the
   advisory's.
3. **Frontend manifests (D2)** — worth building now, or does it stay
   parked behind the collector work? If built: does the SPA get its API
   base URL at build time (a second image per environment) or at runtime
   (an nginx-substituted `config.js`)? That decision shapes the chart.
4. **`architecture.md`'s slice 5 (§4, top row)** — delete the 40 lines
   describing the removed editors, or rewrite them as a short "what this
   was, and why it was removed" paragraph? The removed design is
   genuinely interesting (`ShadowPanel`, the preview-endpoint-as-source-
   of-truth rule) and there is no ADR capturing it (M3).
5. **The runtime diagram** — regenerate it with OneView and OpenManage,
   or retire it? It is pinned to a stale revision and nothing regenerates
   it automatically, so it will go stale again.
6. **ADR front matter (M1)** — worth a normalising pass across all 22,
   or is a consistent format for *new* ADRs enough?
7. **`scripts/dev-up.sh` (C2)** — keep it as a Podman-only fallback and
   fix the error message, or make it genuinely runtime-agnostic? Given
   `docker compose` is now the documented preferred path, Podman-only
   seems right.
