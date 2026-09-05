# Tooling worth adding — a measured evaluation

Research note, 2026-09-06. Branch `dev-refactor`. **No source, `pyproject.toml`,
`package.json` or CI file was changed to produce this** — every tool below was
run ad hoc (`uv run --with <tool>`, `npx --yes`) against the tree as committed
at `f9ab059`.

The governing rule for every verdict here is the user's: **do not add a tool
whose findings you would then ignore.** A hit count is not a reason to adopt
something; a hit count you would act on is.

Baseline: `uv run ruff check .` and `uv run ruff format --check .` and
`uv run ty check backend/app tools` are all clean on this tree, as is
`npm run lint` (oxlint). 23,981 lines of Python across `backend/app` (131
files) and `tools` (8 files), 74 test files.

---

## The bottom line first

### Add, and gate CI

| Tool / rule | Why | Cost to get green |
|---|---|---|
| **`import-linter`** | The only thing here that can see a layering violation at all. 4 of 5 contracts written from CLAUDE.md's own stated architecture **hold today**; the 5th is broken 8 ways and is worth knowing about. Runs in ~2s. | One dev dependency, a ~35-line `.importlinter`, and a decision on the broken contract (fix, or write it down as `ignore_imports`). Plus one missing `__init__.py` — see below. |
| **`ruff --extend-select PTH,RET,PERF,FURB,TRY004,TRY300,TRY400,C901`** | 27 findings total across the whole repo, every one a true positive, three autofixable. `PTH` and `LOG` are already at **zero** — free ratchets. | 27 edits, or fewer with `--fix`. |
| **`deptry --known-first-party app`** | 5 findings, all real: `starlette` is imported directly in 3 modules but declared nowhere. | Add `starlette` to `[project.dependencies]` (pinned), or stop importing it directly. |
| **`pip-audit`** — already a documented chore, promote it | It is **not clean any more**, and nothing noticed. See the currency report. | Bump `cryptography`. |

### Add as a documented local command, never a gate

| Tool | Why not a gate |
|---|---|
| **`vulture`** (default confidence) | Found 14 genuinely dead symbols left behind by the read-only refactor — real value. But ~85% of its 160-odd findings are FastAPI route handlers and Pydantic validators it cannot see are called. A gate would need a whitelist file that is itself maintenance. |
| **`knip`** (frontend) | Same story, same refactor: 1 unused file, 18 unused exports. Its 38 "unused exported types" are mostly response-shape types used structurally — noise. |
| **ruff `D` (pydocstyle)** | Directly serves convention 8, but the repo is 267 docstrings and 225 `D213` deviations away from green, and CLAUDE.md explicitly says convert a file when you touch it rather than sweeping. Gate it **per directory** as the conversion lands (see the `D` section — the shipped `convention = "google"` is *wrong* for this repo and will fight you). |

### Do not add

| Tool | Why |
|---|---|
| **`bandit`** | 12 findings, every one a duplicate of a ruff `S` code already selected, 11 of which already carry a reasoned `# noqa: S…`. Its only unique output is two extra false positives. A second security linter that reports what the first one already suppressed is pure cost. |
| **`radon` / `xenon`** | Its maintainability index is **flat** on this codebase — every single file ranks A, worst 30.15 — because MI is dominated by comment ratio and this repo is comment-heavy by policy. Zero signal. Its cyclomatic complexity diverges wildly from ruff's mccabe for structural reasons (below), so running both means arguing about which number is real. `C901` alone suffices. |
| **`deadcode`** | `vulture` already answers this question. Two dead-code tools is one dead-code tool and a second opinion nobody reads. |
| **`rollup-plugin-visualizer` / a size budget** | The bundle is 479 kB raw / **140 kB gzip**, one chunk, for an air-gapped intranet admin UI served over a LAN. Vite already warns at 500 kB and does so for free. |
| **ruff `COM`, `EM`, `PLR2004`, `PLR0913`, `ARG`, `SLF`, `ERA`, `TRY003`** | Measured below. Between them, ~1,100 findings that are style churn, deliberate design, or outright false positives on this codebase. |

---

## Ruff: rules not currently selected

Current config selects `E, F, I, UP, B, C4, SIM, RUF, ASYNC, S`, plus `ANN`
code-by-code (`ANN001-003`, `ANN201-206`), ignoring `S101`, with `tests/**`
also ignoring `S105/S106/S311`. So `B`, `SIM`, `RUF`, `ASYNC`, `S` and most of
`ANN` are **already on** and were not re-evaluated.

Every count below is `uv run ruff check --select <RULE> --statistics .` over
the whole repo (backend + tools + tests) unless stated.

### Already at zero — free ratchets, add them and forget them

| Rule | Hits | Note |
|---|---|---|
| `PTH` (flake8-use-pathlib) | **0** | Nothing in the repo uses `os.path`. Costs nothing, stops it starting. |
| `LOG` (flake8-logging) | **0** | Structurally inapplicable: logging is `structlog` everywhere; stdlib `logging` appears in exactly one file (`app.infrastructure.logging.config`, which configures it). Free, but buys little. |
| `DTZ`, `ICN`, `ISC`, `PIE`, `Q`, `RSE`, `TID`, `A`, `PD`, `PGH`, `NPY` | **0** each | Same reasoning. `DTZ` at zero is worth noting given ADR-0006's timezone bug — the codebase is already clean of naive-datetime constructors. |

### Worth adding — small, real, mostly autofixable

| Rule | Hits | Sample | True positive? |
|---|---|---|---|
| `RET` | **1** | `RET501` in `tests/unit/infrastructure/providers/test_openmanage_provider.py:105` | Yes, trivial, autofixable. |
| `FURB` | **2** | `FURB188` at `backend/app/infrastructure/providers/openmanage/client.py:280` (hand-rolled `removeprefix`); `FURB167` in a test | Both yes, both autofixable. |
| `PERF` | **5** (`PERF401`) | `intersight/mapping.py:523,527`; `ucs_central/provider.py:421`; 2 in tests | Yes. Three are in collector hot paths that run per-server at 10k scale, which is where this repo's stated scale target actually lives. |
| `TRY004` + `TRY300` + `TRY400` | **11** | `TRY400` at `redfish/provider.py:259,364` and `tools/run_collector.py:945` — `logger.error` in an `except` block, losing the traceback | **Yes, and `TRY400` is the one that matters.** A collector that swallows a traceback is exactly the failure this platform is worst at diagnosing. |
| `C901` @ `max-complexity = 15` | **4** | `intersight/provider.py:373 _build_joins` (19), `tools/verify_intersight.py:186 _inspect` (17) and `:496` (16), `tools/verify_ucs_central.py:64 _run` (17) | Yes. At the default 10 it is 10 hits (9 in `backend/app` + `tools`), which is also defensible. |

Combined, `--extend-select PERF,RET,PTH,LOG,FURB,TRY004,TRY300,TRY400,C901`
yields **27 findings** repo-wide (34 including the `ERA`/`PLW` codes rejected
below). That is one commit, and then a permanent gate.

### Rejected, with the measurement

| Rule | Hits | Verdict |
|---|---|---|
| `COM812` | **553** | Never. It conflicts with `ruff format`, which owns trailing commas. |
| `EM101`/`EM102` | **183** | Style churn. Every hit is `raise X(f"...")`, which is this repo's consistent, readable idiom. |
| `TRY003` | **178** | Same objection, same sites. This is why `TRY` must be selected code-by-code and not by prefix. |
| `PLR2004` (magic value) | **176** | Noise on a codebase full of byte/MiB conversions and HTTP status comparisons. |
| `PLR0913` (too many args) | **43** | Every hit is a keyword-only constructor or a mapping function. Deliberate. |
| `ARG` | **42** | 39 in tests (fixtures, protocol-conformance stubs, `lambda *_:` fakes). 1 real-ish hit in `redfish/provider.py`. Not worth the suppressions. |
| `SLF001` | **13** | All 13 in tests, all deliberately reaching into a client's internals to assert on it. Pure FP here. |
| `ERA001` | **3** | **3/3 false positive.** All three are the `INVENTORY_SITES` / `INVENTORY_GPU_MODELS` / `INVENTORY_NIC_OS_NAMES` worked examples inside `backend/app/config/settings.py`'s comments. Ruff reads a documented env-var value as commented-out code. |
| `PLW0406` (import-self) | **1** | **1/1 false positive.** `backend/app/infrastructure/logging/config.py` does `import logging.config` — the stdlib module — and ruff sees the filename collision. |
| `PLC0415` | **4** | All four are deliberate in-test imports. |
| `PLW2901` | **1** | `tools/run_collector.py:802` rebinds `gpu` in a loop. Real but cosmetic. |
| `N818` | **1** | `RegexTimeout` should be `RegexTimeoutError`. Real, but it is a public domain-port name and renaming it is a change, not a lint fix. |
| `INP001` | **2** | **One of these is a real finding** — see the next section. |
| `ANN401` | **43** in `backend/app` + `tools` (111 repo-wide) | Stays off. ADR-0019's reasoning holds; the count has grown from the 37 CLAUDE.md records, which is the collectors landing, not drift. |

### `D` (pydocstyle) — the shipped Google convention is wrong for this repo

This is the most interesting ruff result, because the obvious configuration is
the wrong one.

`convention = "google"` enables **`D212` (multi-line-summary-first-line)** and
disables `D213`. CLAUDE.md convention 8 mandates the opposite shape:

```python
def get_user(user_id):
    """
    Get a user by ID.
    ...
```

— summary on the **second** line. So `convention = "google"` fires `D212`
**319 times inside the files CLAUDE.md says are already converted**
(`backend/app/infrastructure/providers/**` + `tools/**`). Adopting it as
shipped would tell you to undo the convention.

The configuration that actually encodes this repo's rule is
`convention = "google"` + `extend-select = ["D213"]` + `ignore = ["D212"]`.
Measured with that:

| Scope | Total | `D213` (wrong summary position) | Missing docstrings (`D101/2/3/4/7`) | `D205` | `D417` |
|---|---|---|---|---|---|
| `backend/app` + `tools` | **635** | 225 | 267 | 133 | 2 |
| `providers/**` + `tools` (the "already converted" set) | **122** | 53 | 16 | 49 | 1 |

Three things follow:

1. **Even the converted files are not green** (122 findings), so this cannot be
   switched on as a whole-repo gate today.
2. `D213` is autofixable — 225 mechanical edits, one commit. `D205` (133) is
   not: it fires wherever a summary wraps to two lines, e.g.
   `fake/generator.py:690`, and fixing it means rewriting summaries.
3. `D417` — the rule that actually checks every `Args:` entry exists — finds
   only **2**. The convention is being followed where docstrings exist; what is
   missing is docstrings, 267 of them.

**Recommendation:** document the command, do the `D213 --fix` sweep as its own
commit, and gate `D` **per directory** via `per-file-ignores`, shrinking the
ignore list as convention 8's file-by-file conversion proceeds. Do not gate it
whole-repo, and do not use bare `convention = "google"`.

---

## `import-linter` — the highest-value item, and it found something

Contracts written directly from CLAUDE.md's stated architecture, then run
(`lint-imports`, grimp analysed 132 files / 343 dependencies):

| Contract | Result |
|---|---|
| Layers `app.api` > `app.application` > `app.domain` | **KEPT** |
| `app.domain` never imports `app.config` (the Settings rule, ADR-0018) | **KEPT** |
| `app.domain` never imports `app.infrastructure` / `.api` / `.application` / `.dependencies` / `.middleware` | **KEPT** |
| `app.infrastructure` never imports `app.api` / `.application` / `.dependencies` | **KEPT** |
| `app.application` never names a concrete adapter (`app.infrastructure`) | **BROKEN — 8 imports** |

So **the layering rules the repo actually documents all hold today.** The
SiteCatalog threading works; `app.domain` reaches nothing but `app.utils` and
`app.errors`, both leaves. That is worth locking in before it stops being true.

The broken contract is the stricter one nobody has written down, and all 8
violations are the same shape — the application layer typing its constructor
parameters with concrete Mongo repositories instead of domain ports:

```
app.application.services.audit_service          -> app.infrastructure.mongodb.audit_event_repository        (l.18)
app.application.services.bootstrap              -> app.infrastructure.mongodb.classification_rule_repository (l.27)
app.application.services.bootstrap              -> app.infrastructure.mongodb.health_policy_repository       (l.31)
app.application.services.classification_service -> app.infrastructure.mongodb.classification_rule_repository (l.61)
app.application.services.classification_service -> app.infrastructure.mongodb.client                         (l.64)
app.application.services.classification_service -> app.infrastructure.mongodb.indexes                        (l.65)
app.application.services.health_policy_service  -> app.infrastructure.mongodb.health_policy_repository       (l.41)
app.application.services.health_policy_service  -> app.infrastructure.mongodb.server_repository              (l.42)
```

Notes that matter for deciding what to do:

- **`ServerRepository` already exists as a Protocol** in
  `backend/app/domain/ports/repository.py:51`, and
  `health_policy_service.py:195` takes `MongoServerRepository` anyway. That one
  is a one-line fix.
- The other three repositories (audit event, health policy, classification
  rule) have **no port at all**, so "fix" there means introducing three
  Protocols — a real change, not a lint cleanup.
- `classification_service` is the worst of them: it imports `MongoClientHolder`
  and `SERVERS_COLLECTION` and reaches raw Mongo at line 186
  (`self._mongo.db[SERVERS_COLLECTION]`), bypassing the repository entirely.
- `default_system_rules` — a *domain* concern — lives in
  `app.infrastructure.mongodb.classification_rule_repository`, which is what
  drags `bootstrap` across the line.
- `app.application.services.ingest` (the pipeline every collector runs through)
  imports **no** infrastructure. The seam is intact where it counts most.

**Blocker discovered while doing this: `backend/app/infrastructure/__init__.py`
does not exist.** Every other package under `backend/app` has one. The whole
infrastructure layer is an implicit namespace package — which works at runtime
and is exactly what ruff's `INP001` flags at
`backend/app/infrastructure/singleflight.py:1`. It also means grimp cannot see
the package at all: `lint-imports` fails outright with
`Module 'app.infrastructure' does not exist.` The measurements above were taken
against a scratch copy of `backend/app` with an empty `__init__.py` added.
Adding that file is a prerequisite for this tool, and is a good idea regardless.

**Verdict: add it, gate CI.** Ship the four contracts that already pass, plus
either the fifth with an explicit `ignore_imports` list naming those 8 lines
(so the debt is visible and cannot grow), or the fifth after fixing them.

---

## `vulture` — real findings under a lot of noise

| Confidence | Findings | Assessment |
|---|---|---|
| `--min-confidence 100` | **2** | Both false positives: `exc_type` / `tb` in an `__aexit__` signature at `redfish/client.py:235,237`. |
| `--min-confidence 80` | **2** | Same two. |
| default (60), `backend/app tools` only | 142 | Test-only code counted dead. |
| default (60), `backend/app tools tests` | ~160 (174 output lines) | 15 classes, 23 functions, 12 methods, 119 variables, 4 attributes, 1 unreachable-code. |

**The FP source is exactly the predicted one:** FastAPI route handlers
(`liveness`, `readiness`, `list_rules`, `get_server`, `enable_maintenance`, …),
Pydantic validators (`_priority_within_band`, `_mode_is_known`, `_split_csv`)
and Starlette exception handlers (`handle_app_error`) are all invoked by
decorator and look dead. That is the large majority of the output.

**But it found genuine dead code that nothing else here catches.** 15 symbols
were spot-checked with `grep` across `backend`, `tools`, `tests` and
`frontend/src`; **14 have zero external references**:

- `backend/app/api/v1/classification_schemas.py`: `ClassificationRuleCreate`,
  `ClassificationRuleUpdate`, `ClassificationPreviewRequest`,
  `ClassificationPreviewResponse`
- `backend/app/api/v1/health_policy_schemas.py`: `HealthPolicyCreate`,
  `HealthPolicyPreviewRequest`, `HealthPolicyPreviewResponse`
  (`HealthPolicyUpdate` has 1 reference — the only one of the 15 that is live)
- `backend/app/errors.py`: `RevisionConflictError`, `UnauthorizedError`,
  `ForbiddenError`, `RateLimitedError`, `ServiceUnavailableError`,
  `ManagerHasChildrenError`, `InvalidManagerHierarchyError`

The first seven are **leftovers of commits `27b20a8` and `f9ab059`**, which made
rules and policies read-only and removed the write endpoints. The request/response
schemas for endpoints that no longer exist were never deleted. `knip` found the
mirror image of this on the frontend, independently — see below.

**Verdict: local command, not a gate.** Run it after a removal-shaped refactor,
which is precisely when it pays. `uv run --with vulture vulture backend/app tools tests`.

---

## `radon` / `xenon` vs ruff `C901`

**Maintainability index: no signal at all.** `radon mi backend/app tools -n B`
returns **nothing** — every file in the codebase ranks A, and the worst is
`redfish/mapping.py` at 30.15. MI is heavily driven by comment ratio, and this
repo has a documented convention of large docstrings, so MI is measuring the
docstrings. Do not gate on it and do not report it.

**Cyclomatic complexity: 24 functions rank C or worse**, worst being
`intersight/provider.py:373 _build_joins` (E, 34) and
`application/services/ingest.py:460 IngestService._build_server` (**F, 41**).

But the two tools disagree structurally, and the disagreement decides this:

| Function | radon CC | ruff mccabe |
|---|---|---|
| `IngestService._build_server` | **F (41)** | **3** |
| `intersight/provider.py:_build_joins` | E (34) | 19 |
| `health/conditions.py:validate_condition` | C (19) | 15 |

`_build_server` is ~150 lines of straight-line field mapping full of
`x if y else z` and `a or b`. Radon counts every one of those as a decision
point; ruff's mccabe counts branch *statements*. Neither is wrong — they answer
different questions — but you cannot gate on both without adjudicating that
argument on every PR.

`C901` is already in ruff, needs no new dependency, and at
`max-complexity = 15` flags **4** functions. That is the whole recommendation.

**Verdict: don't add radon or xenon.** Add `C901` if you want a complexity gate.

---

## `bandit` vs ruff `S`

`bandit -r backend/app tools`: **12 findings** — 1 High, 4 Medium, 7 Low.

| Bandit | Count | Ruff equivalent | Status in repo |
|---|---|---|---|
| `B501` request_with_no_cert_validation (High) | 1 | `S501` | Already `# noqa: S501` at `intersight/client.py:166`, with the reasoning in the class docstring |
| `B104` hardcoded_bind_all_interfaces (Medium) | 4 | `S104` | All 4 already `# noqa: S104` — three are the `"0.0.0.0"` *unset-IP sentinel*, a documented FP |
| `B311` random (Low) | 4 | `S311` | Already covered by the `tests/**` per-file-ignore and existing noqas |
| `B105` hardcoded_password_string (Low) | 1 | `S105` | Already noqa'd |
| `B106` hardcoded_password_funcarg (Low) | 2 | `S106` | **Ruff does not flag these** — `ManagerConnection(password="")` at `tools/run_collector.py:563,931`, the endpointless-`UCS_MANAGER` sentinel. Both false positives. |

So bandit's entire output is either already-selected-and-already-suppressed
ruff findings, or two additional false positives. Its unique contribution is
negative.

**Verdict: do not add.** ruff `S` is already doing this job, and doing it with
`noqa` comments that carry reasons.

---

## Unused dependencies

### Python — `deptry`

Run bare, `deptry .` reports **432 issues**, all `DEP003 'app' imported but it
is a transitive dependency` — it cannot see that `app` is first-party because
it lives under `backend/` rather than at the root. That is a configuration
problem, not a finding, and is the reason a bare run looks useless.

With `deptry . --known-first-party app --known-first-party tools`: **5
findings, all real, all the same one.**

```
backend/app/exception_handlers.py:22    DEP003 'starlette' imported but it is a transitive dependency
backend/app/main.py:22,23               DEP003 'starlette' ...
backend/app/middleware/request_context.py:21,22  DEP003 'starlette' ...
```

`starlette` is imported directly by name in three modules but appears nowhere in
`[project.dependencies]` — it arrives via `fastapi`. That is the same class of
latent breakage the pinning conventions exist to prevent: a FastAPI major that
re-vendors or renames Starlette breaks these imports with no diff anywhere.

**`DEP002` (declared but never imported) is zero.** The `python-multipart`
situation that ADR-0013 records has not recurred — nothing declared in
`pyproject.toml` is unreached today.

**Verdict:** worth running, worth fixing (one line). Gate it only after the
`--known-first-party` config is committed and the starlette declaration lands,
at which point it is free.

### Frontend — `knip`

`npx knip` with no configuration:

- **Unused files: 1** — `frontend/src/api/healthMetrics.ts`
- **Unused exports: 18** — including `RULE_SOURCES_FOR_CREATE`,
  `emptyRuleScope`, `defaultRuleFlags`, `emptyPolicyScope`,
  `operatorsForMetricType`, `POLICY_CATEGORIES`, `listServerEvents`
- **Unused exported types: 38**
- **Unused dependencies: none** — every entry in `frontend/package.json` is reached

The 18 unused exports are the **frontend half of the same refactor vulture found
on the backend**: `*_FOR_CREATE`, `empty*Scope`, `default*Flags` and the operator
tables are the machinery of the rule and policy *editors*, which commit `27b20a8`
deleted. Two independent tools, two languages, one leftover.

The 38 "unused exported types" are largely FPs in spirit — API response shapes
that are used structurally inside other types in the same module and simply
never imported by name. Do not act on that section without reading it.

**Verdict: local command.** `npx knip` after any page removal. Not a gate: the
types section would need a config file to silence, and 0 unused dependencies
means there is nothing here to protect.

---

## Frontend bundle size

`npm run build` (Vite 8.2.1, 258 modules):

```
dist/index.html                   0.46 kB │ gzip:   0.30 kB
dist/assets/index-BXv_QRWa.css   27.16 kB │ gzip:   6.20 kB
dist/assets/index-D6rYWdvw.js   478.87 kB │ gzip: 140.59 kB
```

One JS chunk, no code splitting, **140.59 kB gzipped** for React 19 +
`@tanstack/react-query` + `@tanstack/react-table` + `react-router` + Tailwind 4.

This is an admin UI served from an nginx pod to operators on a datacenter LAN,
not a public site with a bounce-rate problem. 140 kB gzip is not a problem worth
a plugin, a CI job and a threshold argument.

Worth knowing, though: **Vite's default `chunkSizeWarningLimit` is 500 kB and the
bundle is at 478.87 kB.** The free warning is roughly one dependency away from
firing on its own, which is a better tripwire than anything that would be added
here. If a hard gate is ever wanted, it is three lines of shell against
`du -b dist/assets/*.js` — not `rollup-plugin-visualizer`, which is a
diagnostic for when the number is already bad.

**Verdict: add nothing. Do not raise `chunkSizeWarningLimit` to silence the
warning when it eventually fires.**

---

# CI currency report

The standing chore from CLAUDE.md, run as research only. Nothing was changed.

## 1. Are the action pins current?

Every `uses:` line in `.github/workflows/ci.yml`, checked with
`gh api repos/<repo>/releases/latest` and
`gh api repos/<repo>/git/ref/tags/<tag>`:

| Action | Pinned | SHA verifies to that tag? | Latest release | Status |
|---|---|---|---|---|
| `actions/checkout` | v7.0.1 | yes | v7.0.1 | current |
| `astral-sh/setup-uv` | v10.0.1 | yes | v10.0.1 | current |
| `actions/upload-artifact` | v7.0.1 | yes | v7.0.1 | current |
| `actions/setup-node` | v7.0.0 | yes | v7.0.0 | current |
| `PaulHatch/semantic-version` | v6.0.2 | yes | v6.0.2 | current |
| `docker/login-action` | v4.6.0 | yes | v4.6.0 | current |
| **`docker/setup-buildx-action`** | **v4.2.0** | yes | **v4.3.0** | **behind by one minor** |
| `docker/metadata-action` | v6.2.0 | yes | v6.2.0 | current |
| `docker/build-push-action` | v7.3.0 | yes | v7.3.0 | current |

**All nine pinned SHAs were verified to be the exact commit their `# vX.Y.Z`
comment claims** — no pin has drifted from its label.

One update available, and it **resolves** (the trap CLAUDE.md warns about — this
repo has twice been broken by assuming a rolling major tag exists):

```
docker/setup-buildx-action  v4.3.0 -> 37fe631027851001ddb9b187196cc803df7f5f0e  (object type: commit)
```

## 2. Node runtimes — nothing is being retired

Every action's `action.yml` was fetched **at its pinned SHA** and its `using:`
declaration read. **All nine declare `node24`.** None is exposed to the Node 20
removal that forced the `mathieudutour/github-tag-action` replacement in ADR-0010.

## 3. Vulnerabilities

**`pip-audit` is NOT clean.** CLAUDE.md records both audits clean as of
ADR-0013; that is now out of date.

`uv run --with pip-audit pip-audit --skip-editable` — **11 advisories in 1
package**:

| Package | Pinned | Advisories | Fix versions |
|---|---|---|---|
| `cryptography` | **46.0.3** | PYSEC-2026-35, PYSEC-2026-36, PYSEC-2026-2141, PYSEC-2026-3552, PYSEC-2026-3553, PYSEC-2026-3554, GHSA-537c-gmf6-5ccf | 46.0.5 / 46.0.6 / **46.0.7** / 48.0.1 / 49.0.0 / **50.0.0** |

Latest on PyPI is **50.0.1**. Note the split: staying inside 46.x (i.e. `46.0.7`)
clears PYSEC-2026-35/36/2141 but leaves PYSEC-2026-3552/3553/3554 and
GHSA-537c-gmf6-5ccf, which need 48.0.1 / 49.0.0 / 50.0.0. This package is a
*direct* dependency (it signs Intersight requests, ADR-0017) and is not optional,
so step 3 of the chore — "is it actually reached?" — does not offer the
`python-multipart` escape here. **The version the air-gapped mirror carries is
the real constraint and needs checking before picking a target.**

`npm audit` in `frontend/`: **found 0 vulnerabilities.** Still clean.

## 4. Anything unused? — see the deptry section

`DEP002` is zero. Nothing declared is unreached, so there is no repeat of the
`python-multipart` deletion available.

## 5. Base images

| Image | Pinned | Latest | Status |
|---|---|---|---|
| `Containerfile`: `ubi9/ubi-minimal` | **9.8** | **9.8** | **current** |
| `frontend/Containerfile`: `ubi9/nodejs-22` | **`:latest`** | — | **unpinned** |
| `frontend/Containerfile`: `ubi9/nginx-124` | **`:latest`** | — | **unpinned** |

The UBI check took walking 2,477 tags across 25 pages of
`registry.access.redhat.com/v2/ubi9/ubi-minimal/tags/list` — the endpoint caps at
100 tags per page and returns the *oldest* first, so a single unpaginated request
answers "9.4" and is wrong. Highest bare minor published is `9.8`. **The backend
base image is current.**

**Flagged:** `frontend/Containerfile` uses `:latest` for both stages. That is the
mutable-pointer problem ADR-0013 pinned every CI action to avoid, sitting in the
build of one of the two images this repo publishes — a rebuild silently changes
the Node and nginx underneath the frontend with no diff anywhere. Out of scope
for a tooling pass, but it belongs in the same conversation.

## 6. `ty`

Pinned `ty==0.0.76`; latest on PyPI is **0.0.78** (0.0.77 also exists).

**Measured, not assumed:** `uv run --with ty==0.0.78 ty check backend/app tools`
→ **All checks passed.** The bump is free on this codebase. The `[tool.ty.rules]`
severities written down in `pyproject.toml` should still be re-read after
bumping, since those exist precisely because 0.0.76's published docs disagreed
with its binary.

## 7. `ruff` (not part of the documented chore, but the same question)

Pinned `ruff==0.14.0`; latest is **0.16.6** — two minors behind.

**Measured:** with the repo's current config, `ruff 0.16.6`:
- `ruff check .` → **All checks passed**
- `ruff format --check .` → **6 files would be reformatted**

So the bump is *not* free: it carries a formatter change. That is a one-command
sweep (`ruff format .`) but it should be its own commit, not slipped in
alongside a rule change, and it will touch files unrelated to whatever else is
being done.

---

## Reproducing all of this

```bash
# ruff rule surveys
uv run ruff check --select <RULE> --statistics .

# pydocstyle, correctly configured for this repo's mandated shape
#   [lint] select = ["D"]; extend-select = ["D213"]; ignore = ["D212"]
#   [lint.pydocstyle] convention = "google"
uv run ruff check --config <that file> --statistics backend/app tools

# layering — REQUIRES backend/app/infrastructure/__init__.py to exist
uv run --with import-linter lint-imports --config .importlinter

uv run --with vulture  vulture backend/app tools tests
uv run --with vulture  vulture backend/app tools --min-confidence 100
uv run --with radon    radon cc backend/app tools -n C -s
uv run --with radon    radon mi backend/app tools -n B -s      # returns nothing
uv run --with bandit   bandit -r backend/app tools -q
uv run --with deptry   deptry . --known-first-party app --known-first-party tools
uv run --with pip-audit pip-audit --skip-editable

cd frontend && npm audit && npx --yes knip && npm run build
```
