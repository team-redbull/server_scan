# Production-hardening plan — 2026-09-06

Companion to `docs/notes/2026-09-audit.md` (findings, with IDs referenced
here) and the seven `docs/notes/2026-09-research-*.md` files.

**Status: approved 2026-09-06. Phases 1-11 done, committed, and pushed to
`dev-refactor` (`686160f`, `453f47e`+`8dfed16`+`517cfce`, `c90968a`,
`4806d21`+`6066cc5`+`2920510`, `b5d6702`, `37d1cce`, `a669a97`, `8d463b8`,
Phase 9's own commit, Phase 10's own commit, Phase 11's own commit
respectively — Phase 2 shipped as three commits and Phase 4 as three
instead of one, see their own sections for why). Phase 6
also surfaced and fixed an unrelated dev-tooling bug (`d448822`):
`scripts/dev-up.sh down` never removed Mongo's named volume, so `down &&
up` silently kept the previous run's data instead of the empty database
the README documents that sequence as producing. Two small follow-ups
landed after Phase 8 (`27ca909` — the `actions/cache` pin Phase 8 itself
added declared node20, CI's own deprecation annotation caught it on the
very next run; `e38de2a` — docs only) plus one out-of-band, user-requested
feature between Phase 8 and 9 (`734717e`, its own section below,
"Between Phase 8 and 9"): all five collectors now log/print a
`took=`/`collector.run_complete` run duration. **Every phase actually
scheduled in this pass is now done.** Phase 12 stays deliberately
deferred (see its own section).

Ordering follows the brief: contract and architecture first while the diff
is still legible, mechanical sweeps last. One phase = one reviewable
commit. Every phase runs the full convention-7 gate before it is called
done, and the output is pasted into the report.

## Decisions taken (2026-09-06)

| Q | Decision |
|---|---|
| Q1 | **Delete the preview path; keep the validators and re-point them.** `preview()` has zero callers — `f9ab059`'s own message says both `/preview` endpoints "had no caller left once the editors went". But `validate_rule_write`/`validate_policy_write` get wired into `bootstrap.py`, which today seeds the shipped defaults **without validating them**. Since writes are gone, those defaults are the *entire population* of rules and policies, so this is complete coverage — and it closes C12 outright rather than deferring it. |
| Q2 | **Oversight — fix it.** OneView gets `collection_errors`; truncation messages must name the numbers (profiles returned, count requested, the 256 ceiling); the commit body must state that exit 3 is new for OneView and what an operator does about it. |
| Q3 | **Fix the shutdown stall with a dedicated executor**, never joined at shutdown, so `shutdown_default_executor` has nothing to wait for. Not a hard exit (would risk truncating the summary that *is* the run's record) and not `activeDeadlineSeconds` tuning (encodes a bug in cluster config). **Whole-run budget stays deferred** per ADR-0014:496–504 — a deliberate decision, not reopened. |
| Q4 | **In scope.** Compare-and-set, raise the existing `RevisionConflictError`, map to 409, surface in the UI as part of C14. |
| Q5 | **Keep the behaviour; one line in ADR-0002** recording the deliberate exception. |
| Q6 | **Build the ABC** — superseding the research's Protocol recommendation. See Phase 1. |
| Q7 | **UX-1, UX-2, UX-4. Hold UX-3.** |

---

## The gate, run at the end of every phase

```bash
docker compose up -d mongo redis          # `podman compose` (space) does not work here
uv run pytest -q                          # unfiltered — `-m unit` deselects 38%
uv run ruff check . && uv run ruff format --check . && uv run ty check backend/app tools
cd frontend && npm run lint && npm run typecheck && npm run test -- --run && npm run build
npm run test:e2e
```

Then tear the stack down. A stack was already running when this session
began; the performance run left `podman ps -a` empty.

---

## Phase 1 — The provider contract

**Commit:** `feat!: require every collector to report collection_errors and release its session`

### The decision, and why the ABC/Protocol debate was the wrong debate

An ABC, decided 2026-09-06. The research recommended a 6-member
`Protocol` and was overruled on a changed criterion — worth recording,
because the reasoning generalises.

Every argument in the original comparison (runtime `TypeError` vs ty
diagnostic, nominal vs structural, where the check fires) is about
**catching a mistake**. Both options catch it, which is why it kept
reducing to a cost tiebreak.

But OneView's defect was not a wrong shape. Four providers each
hand-wrote the same four lines of error bookkeeping and the author of the
fifth did not. **Nobody forgot to declare something; somebody forgot to
reimplement something for the fifth time.** A Protocol holds no code, so
the best it can do is tell that author they got it wrong — after which
they hand-write the same four lines a fifth time and hopefully get them
right. The ABC is the only construct that lets them not write those lines
at all.

The research's "~none to inherit" measurement is accurate about the code
as it stands and answers the wrong question: it measures what *is*
shared, not what *should* be. The four headline findings are each a place
where a provider was left to get a shared discipline right alone, and one
did not. That measurement describes the bug.

### What the base class owns

```python
class ServerInventoryProvider(ABC):
    def __init__(self) -> None:
        self._collection_errors: list[str] = []

    # inherited — a vendor never writes these
    @property
    def collection_errors(self) -> tuple[str, ...]:
        return tuple(self._collection_errors)

    def record_error(self, message: str) -> None:
        self._collection_errors.append(message)

    async def __aenter__(self) -> Self:
        await self._connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._disconnect()

    # each vendor fills these in
    @abstractmethod
    async def _connect(self) -> None: ...
    @abstractmethod
    async def _disconnect(self) -> None: ...
    @abstractmethod
    async def health_check(self) -> None: ...
    @abstractmethod
    def list_servers(self) -> AsyncIterator[ProviderServer]: ...
```

Run-summary logging (start/end/counts) moves in too, replacing five
near-identical hand-rolled versions — same argument.

### Three subtleties, and the mechanism for the ADR

1. **`list_servers` MUST stay `def`.** The rule looks like a typo, so the
   *mechanism* goes in the ADR verbatim, not just the rule:

   ```python
   async def f() -> AsyncIterator[str]: ...        # no yield  -> CoroutineType[..., AsyncIterator[str]]
   async def f() -> AsyncIterator[str]: yield x    # has yield -> AsyncIterator[str]
   ```

   Both are `async def` with the same annotation; a `yield` in the body
   decides the meaning, and a stub cannot have one. So an `async def`
   abstract method promises "await it, then iterate" while all seven
   implementations deliver "iterate", and ty 0.0.76 rejects every one with
   a Liskov violation. The current Protocol already gets this right and
   nothing says why — which is the real danger: it reads as an oversight,
   so a future session "fixes" it and breaks all seven at once. One-line
   comment on the signature, mechanism in the ADR.
2. **`_NameFilteredProvider` is a wrapper, not a vendor.** It must
   inherit (an ABC is nominal) while **delegating** `collection_errors` to
   the provider it wraps, never accumulating its own — otherwise a
   filtered run silently swallows every error, the exact failure class
   this refactor exists to remove. Today it already delegates via
   `collection_errors_of(self._inner)`; under the ABC, `__init__` gives it
   its own empty list and the inherited property would return that.
   **Verify empirically once the base exists** rather than trusting the
   reading.
3. **`__aexit__` params positional-only** (or `*exc: object`), else the
   spelling `oneview/client.py:165` already uses is rejected.

### The failure mode being accepted, knowingly

Every provider becomes coupled to one base class. The day a vendor
genuinely does not fit the lifecycle, the temptation is to bolt an
optional hook onto the base rather than admit the misfit. **Watch for the
base class growing optional hooks — that is this design's disease.**
Chosen anyway because it is slower and more visible than the current
disease: five providers quietly diverging on the same bookkeeping.

### Three spellings that are load-bearing (all verified, not inferred)

1. **`list_servers` stays `def`.** Every provider is an async *generator*.
   An `async def … -> AsyncIterator[X]` stub types as
   `Coroutine[..., AsyncIterator[X]]` and ty rejects **all seven** with a
   Liskov violation. Nothing currently marks this; it is the single
   highest-risk line in the refactor.
2. **`__aexit__` params must be positional-only (`/`)** — otherwise the
   idiomatic `async def __aexit__(self, *exc: object)`, which
   `oneview/client.py:165` **already writes**, is rejected.
3. **`collection_errors` must be a read-only property**, not a protocol
   variable — a `@property` does not satisfy a variable, which would break
   all four existing implementers.

### What changes

- Rewrite `domain/ports/provider.py`. Its current module docstring is
  also stale ("will implement later" — all seven exist) and is itself a
  convention-8 violation; the prose moves to the new ADR.
- Add `__aenter__`/`__aexit__` to all seven providers (**none has one
  today**), absorbing the five duplicated "logout, swallow, warn" blocks.
- Add `collection_errors` to `fake`, `ucs_manager` and **`oneview`** (C1).
- Delete `collection_errors_of` (`run_collector.py:517`) **and** the
  copy at `openmanage/provider.py:205`. Its docstring's argument — that
  this is "ceremony for a single vendor's shape" — is falsified by a
  second vendor needing it.
- Repair `oneview/provider.py:355`'s bare `except` (M5) so a failure is
  recorded, not just counted.
- Add an injected-failure test per fan-out collector (T4). **No type
  construction catches a swallowed `except`** — this is the only thing
  that would have caught C1.
- ADR-0023: the decision, the alternatives, the per-provider migration.

### What could break

Everything that constructs a provider: `tools/run_collector.py`
(2 call sites, `:874` and `:693`), the seeder, and ~10 test stubs.

**Extending `ty` to `tests/` ships in this phase and is a deliverable, not
a nice-to-have.** Those stubs sit outside `ty check backend/app tools`, so
they are checked by nobody, and they carry mypy-style
`# type: ignore[...]` that ty does not understand — **those suppressions
have been suppressing nothing.** Fixing the batch of errors ty surfaces
is the point of the change, not scope creep.

Vendor mapping logic is **not** touched. The constraint holds: this
restructures the contract around the mappings, not the mappings.

### Verified by

Full gate, plus: a `--dry-run` per collector; the new injected-failure
tests; and confirming `oneview` now reaches **exit 3** on a truncated run
where it previously returned 0.

---

## Phase 2 — Concurrency and lifecycle correctness

**Commit:** `fix: stop a disconnecting client from failing every coalesced /servers request`

C5 (`singleflight` — `asyncio.shield`), C6 (Redfish timeout across a
generator yield), C7 (UCS Central task cancellation / session leak),
C9 (OpenManage errors lost on early close), C10 (OneView bare `gather`),
M11 (pointless `async`).

**C8, restated correctly.** The earlier framing ("no call deadline") was
wrong: `ucs_central/client.py:112` already wraps every SDK call in
`asyncio.wait_for(asyncio.to_thread(...))`, exactly as ADR-0014:115 says,
and ADR-0014:496–504 deliberately defers the whole-run budget. The real
defect is that `wait_for` cancels the *await* but cannot cancel the OS
thread: the blocking `ucscsdk` call keeps running, the collector correctly
reports "timed out after Ns" and continues, and then `asyncio.run()` →
`Runner.close()` → `loop.shutdown_default_executor()` waits on the
abandoned thread for `asyncio.constants.THREAD_JOIN_TIMEOUT` = **300 s**
(verified on this project's 3.13.12). One wedged domain stalls the pod for
five minutes *after* it has finished and reported.

**Fix:** give the client its own `ThreadPoolExecutor` and never join it at
shutdown, so `shutdown_default_executor` has nothing to wait for. The
abandoned thread dies with the process — which already happens; we simply
stop paying five minutes to watch it not finish. The whole-run budget
stays deferred.

**Could break:** C6 and C10 change streaming shapes; C8 changes how a
wedged domain terminates. Each needs a test that fails before the fix.

**Verified by:** the agent's repros, converted into regression tests, plus
the full gate.

---

**Correction, 2026-09-06, before implementation.** The "give the client
its own `ThreadPoolExecutor`" fix above is **factually wrong**, confirmed
by reading `concurrent.futures.thread` source directly and by running it,
not by re-reasoning about it: a dedicated `ThreadPoolExecutor`'s worker
threads are still non-daemon (`ThreadPoolExecutor.__init__` has no
argument to make them otherwise) and still register in the *same*
module-global `_threads_queues` every executor's workers do — joined with
**no timeout at all** by `_python_exit()` (hooked via
`threading._register_atexit`, which runs during interpreter shutdown,
*after* `asyncio.run()` has already returned). So this fix does not
remove the 300-second stall — it removes the 300-second *bound*, turning
a five-minute stall into an unconditional hang. Measured directly: both
the default executor and a dedicated one hang indefinitely once a call
wedges; only a manually created **daemon thread** (`threading.Thread`,
never through any `Executor`) exits promptly, since daemon threads are
never registered in `_threads_queues` and the interpreter does not wait
for them at all.

The actual fix shipped in `backend/app/infrastructure/blocking.py`
(`run_abandonable`): a daemon thread bridged back to the event loop via
the public `asyncio.wrap_future`. It also adds a "poisoned client" policy
neither this plan nor the original audit anticipated — see
`docs/cisco-collectors.md`'s "Timeouts, abandoned threads and poisoned
clients" for why a client whose deadline has fired must refuse every
further call rather than risk a second thread touching the same SDK
session. See the commit `fix: run the blocking Cisco SDK calls on
abandonable daemon threads` and its full reasoning there.

---

## Phase 3 — Security and supply chain

**Commit:** `fix: allow INVENTORY_CURSOR_SECRET to be set in Helm and refuse the dev default in production`

S1 (cursor secret settable + production fail-fast), S2 (`cryptography`
bump — regenerate `requirements.txt` **and** `pylock.toml`), S3 (add
`pip-audit` and `npm audit` CI steps), S4 (Prometheus `"<unmatched>"`
label), S5 (`SecretStr` + widen `_drop_sensitive_keys`), S6 (pin
`frontend/Containerfile`), S7 (level up the five workloads).

Small and independent; placed early because S2 and S4 are one-liners with
real value and nothing else depends on them.

**Could break:** the production fail-fast will refuse to start an
install that currently starts. That is the point, but it must be release-noted
as breaking, and `deploy/` must ship the `secretKeyRef` in the *same*
commit or an upgrade wedges.

---

## Phase 4 — API and domain correctness

**Shipped as three commits, not one** (`4806d21`, `6066cc5`, `2920510`):
the dead-code deletion/bootstrap-validation change, the compare-and-set
change, and the small mongo/collector/docs fixes each stood on their own
enough to review separately, the same reasoning Phase 2 gives for its own
three-commit split.

- `4806d21` — `feat: validate shipped classification rules and health
  policies at startup` (M1, C12, C11)
- `6066cc5` — `fix: reject a concurrent server edit instead of silently
  overwriting it` (C13)
- `2920510` — `fix: log and count MongoDB ping failures, and keep
  --dry-run off the database` (M6, M7, M8, M9, M10, M3, Q5)

The Q1 decision makes this phase bigger and better than drafted.

- **C12 is closed, not deferred.** `validate_rule_write` and
  `validate_policy_write` move into `bootstrap.py`, which today seeds the
  shipped defaults with no validation at all (`validate` appears nowhere
  in that file). Because writes are gone, those defaults are the *entire
  population* of rules and policies that can ever exist — so validating
  them at startup is total coverage, and it turns ~35 existing unit tests
  from dead-code tests into tests of the guard that protects the "every
  deployment scores identically" guarantee. Fail loudly at startup on a
  malformed default.
- **Delete the preview path** — `classification_service.py:198`,
  `health_policy_service.py:222`, and the draft-preview request schemas at
  `classification_schemas.py:139` / `health_policy_schemas.py:136`. Zero
  callers; `f9ab059`'s own message records that both `/preview` endpoints
  "had no caller left once the editors went".
- **C13 (Q4): compare-and-set** on `revision`, raise the existing
  `RevisionConflictError` (`errors.py:127`, currently never raised), map
  to 409, and surface it in the UI as part of C14 — a silently-lost
  maintenance toggle is exactly the bug class this platform's rules exist
  to prevent.
- C11 (bootstrap re-sync of drifted policies), M6/M7 (`mongo.ping_failed`
  gains `error=`/`exc_info`; `mongo_ping_failures_total` finally gets
  incremented), M8 (bind run context so `ingest.completed` carries
  `manager_type`), M9, M10, M3, M1.
- **Q5:** one line in ADR-0002 recording `/health/ready`'s plain-dict body
  as a deliberate exception, so the next session does not "fix" it.

---

## Phase 5 — Performance, measured only

**Shipped as `b5d6702`:** `perf: stop re-reading classification rules and
health policies once per server`.

P1 (~20,000 collection reads per 10k run → load once per run;
`ClassificationService`/`HealthPolicyService` each gained a
`load_ruleset`/`load_policies` + `classify_with_ruleset`/
`evaluate_with_policies` pair alongside the unchanged
`classify_server`/`evaluate_server`, and `IngestService.ingest` calls the
load-once pair exactly once per run — verified with a counting-repo test
that fails against the pre-fix code, 25 calls for 25 servers, and passes
after, one call per collector's own `ingest()` invocation), P2 (async
dependency functions, +0.42 ms/request measured — every provider in
`app.dependencies` and every router's private `_xxx` dependency function
is now `async def`; return cached bytes without the decode/re-validate/
re-encode round trip, 0.919 ms — `CacheClient.get_raw` plus a raw
`Response(media_type="application/json")` on a cache hit for
`GET /servers`, `GET /servers/facets` and `GET /servers/{id}`).

**The rule for this phase:** every change carries a before/after number in
the commit body, from the method in
`2026-09-research-performance.md`. Anything that does not move a
measurement does not get made. Explicitly **not** doing: `ORJSONResponse`
(measured as a regression — it disables FastAPI 0.141.1's `dump_json`
fast path) and any middleware change (+0.07 ms; the profiler's 79%
cumulative is an artefact).

---

## Phase 6 — Frontend correctness

**Shipped as `37d1cce`:** `fix: stop showing "all healthy" for a site
whose servers were never evaluated`. All of C2, C3, C14, C15, C16, P3,
the missing error boundary, the sticky-header defect and the `<select>`
accessible-name defect landed in one commit, plus the `unread_fields`
legibility fix (a shared `Reported`/`UnconfirmedMarker` component used by
both `HardwareTab` and `NetworkTab` now). Verified: unit tests (64
passed), `tsc -b`, `oxlint`, `vite build`, and the full Playwright E2E
suite (9 passed) against a freshly wiped and reseeded dev database.

Also fixed, found while reseeding for the E2E run rather than planned:
`scripts/dev-up.sh down` never removed Mongo's named volume, so a
"wiped" dev database was actually 50,000 servers left over from an
earlier performance run — shipped separately as `d448822` since it is
dev tooling, not frontend.

C2 (all-healthy), C3 (`NetworkTab` unread — copy `HardwareTab`'s
treatment, which is already right), C14 (silent maintenance-write
failure), C15 (`invalidateQueries`), C16 (dead result count), P3 (facets
query key), the missing error boundary (`main.tsx`/`router.tsx` — a
render throw blanks the whole app; this is commit `1a896af`, which
`e2e/unread-fields.spec.ts` exists to catch), the sticky header
(`InventoryTable.tsx:126` — `overflow-hidden` becomes the sticky
container and never scrolls, so the comment at `:128` describes behaviour
the code lacks), and the `<select>` accessible-name defect
(`InventoryPage.tsx:222-318`).

Also: make `unread_fields` legible to keyboard and screen-reader users —
it is `title=`-only and likely sub-contrast today.

**Not doing: virtualisation.** The API caps `page_size` at 200 and the
cursor is opaque, HMAC-signed and forward-only, so a 10k virtual list
means 50+ strictly sequential round-trips and a scrollbar that lies about
its data source. The table renders 50 rows; there is no DOM problem.
Raising page size and moving to `useInfiniteQuery` is the real fix.

---

## Phase 7 — Operator UX  (UX-1, UX-2, UX-4 approved; UX-3 held)

- **UX-1 (S)** — `HealthSummary` carries seven severities;
  `OverviewTab.tsx:44` renders one. The page opened to answer *why is this
  unhealthy* never says which category is. The data is fetched every
  request and thrown away. ~20 lines.
- **UX-2 (S)** — link each site card's "3 critical" to
  `?health_overall=CRITICAL`. Three clicks become one.
- **UX-3 (M)** — a History tab from `listServerEvents` (written, called by
  nothing); `HEALTH_STATUS_CHANGED` already records from/to.
- **UX-4 (S)** — "clear filters", a summary of what is applied, and an
  empty state that names the active filters. Facet counts are the app's
  best existing idea and this completes them.

---

## Phase 8 — Tooling and CI

**Shipped as `ci: enforce the layered architecture with import-linter`.**
Findings had drifted from this section's counts (written before this
session's Phase 6/7 work landed) — re-measured rather than trusted:

- **M4 first** — added `backend/app/infrastructure/__init__.py`.
  import-linter cannot see the package until this exists. Side effect
  worth knowing: `app.infrastructure` was previously an implicit
  namespace package, and that alone was silently defeating ruff's isort
  first-party detection for the *entire* `app.*` tree — adding this one
  file surfaced two pre-existing, real `I001` violations
  (`redis/client.py`, `redis/cache.py`) that `ruff check .` had never
  actually been able to see. Fixed alongside.
- `import-linter`, gating **the 4 contracts that hold** (re-derived as
  5 `forbidden`-type contracts, not layers — domain independent of
  application/infrastructure/api; infrastructure independent of
  application and of api; application independent of api). The 5th —
  application never naming a concrete adapter — is broken by exactly
  5 imports across 4 files (`audit_service.py`, `bootstrap.py` ×2,
  `classification_service.py`, `health_policy_service.py`, all reaching
  past `ServerRepository`'s Protocol for a concrete `Mongo*Repository`,
  three of which have no port at all). Recorded as known debt with a
  `ponytail:` marker in `pyproject.toml`, not gated.
- `ruff --extend-select PTH,LOG,RET,PERF,FURB,TRY004,TRY300,TRY400` —
  19 findings (not 27; `PTH`/`LOG` were already clean), all fixed. 4
  autofixable, 2 more via `--unsafe-fixes` (both `TRY400`, both genuine:
  `logger.error` inside an `except` in the Redfish provider and
  `run_collector.py`'s UCS_MANAGER-not-configured path were both losing
  the traceback). One `PERF401` (`ucs_central/provider.py`) kept as
  `# noqa` with a comment: the flagged loop's list is read mid-iteration
  by the `except` clause for a partial-progress count, which a
  comprehension has no way to expose.
  **`C901` (complexity) deliberately NOT added**, unlike the original
  plan: it hits `validate_condition` (ADR-0005's health-policy engine),
  Intersight's join-table builder and OneView's PSU mapping — real
  branching from a genuinely complex external contract, not sprawl.
  Splitting those is its own reviewed change with real regression risk
  (no live hardware to re-verify most of them against), not a mechanical
  lint fix.
- `deptry --known-first-party app` — 5 findings, all one root cause:
  `starlette` imported directly in three files while resolving only as
  fastapi's transitive dependency. Made direct and pinned to what
  fastapi 0.141.1 actually resolves (`1.6.0`). Gated in CI as an
  ephemeral `--with` install, matching `pip-audit`'s pattern.
- `oxlint --deny-warnings` now gates — it was exiting 0 with
  `no-floating-promises` enabled by `typeAware` but never denied.
  Verified type-aware linting genuinely runs (a scratch floating-promise
  file was caught); the real tree has zero warnings today, so this was
  a pure config fix, no source change needed.
- Bumped `ty` 0.0.76 → 0.0.78 (measured clean) and
  `docker/setup-buildx-action` v4.2.0 → v4.3.0.
- Documented, not gated (README's Tests section): `uvx vulture` (dead
  code), `npx knip` (unused TS exports — currently the F-07 dead code
  from the removed rule/policy editors), `ruff check --select D`
  (docstring coverage).
- **Still explicitly not adding**, unchanged from the original plan:
  `bandit` (duplicates already-selected ruff `S`), `radon`/`xenon` (MI
  rewards comment ratio, meaningless on a comment-heavy-by-policy repo),
  `deadcode`, bundle-size tooling.
- `ruff` 0.14.0 → 0.16.6 (reformats 6 files) still deferred — its own
  commit, unchanged.

**CI speed, done in the same phase since it touches the same file**
(user request, not in the original plan): `actions/cache` for the
chromium headless shell in the e2e job, keyed on the resolved
`@playwright/test` version — was re-downloaded on every run, the
slowest single step in that job. Measured on the next push: the browser
install step dropped from ~10s to skipped entirely on a cache hit.
`actions/cache`'s first pin (v4.3.0) declared `node20`; CI's own
annotation flagged the deprecation on the very next run, bumped to
v6.1.0 (`node24`) immediately — left in as a reminder that a brand-new
pin is not exempt from the same staleness this phase's "Keeping CI
current" pass exists to catch. `uv sync --all-groups --locked`
everywhere `uv sync` runs, so a `uv.lock` that drifted from
`pyproject.toml` fails loudly instead of CI silently re-resolving a
different dependency set than any developer's local one. Everything
else was already right: `enable-cache: true` (uv) and `cache: npm`
(node) are both already keyed on their lockfiles, the four top-level
jobs already run concurrently with no artificial `needs` between them,
and the Docker builds already use `cache-from/to: type=gha`.
**Considered and deliberately not done:** `pytest-xdist` for the `test`
job — real potential win, but that job's `mongo`/`redis` are fixed
service containers shared by every test on `localhost`, not
per-test-isolated `testcontainers`, so parallel workers risk real
cross-test collisions rather than just flaking. Needs a look at the
suite's isolation guarantees first, not a blind flag flip.

---

## Between Phase 8 and 9 — collector run duration (user request, not in the plan)

**Shipped as `feat: log and print collector run duration as took=/collector.run_complete`.**
Every collector except Intersight logged nothing about how long its run
took, and Intersight's own `intersight.run_summary`'s `seconds=` field was
a one-off nobody else followed. All five now share one convention:

- `tools/run_collector.py` gets a `_format_duration(seconds: float) -> str`
  helper next to `_format_capacity`/`_format_tb`/`_format_disk_size`:
  `"42.3s"` below a minute, `"2m 13s"` at or above it.
- Timed at the two places that actually drive a provider end to end —
  `_dry_run_one_manager` (the whole function body, `try`/`finally`) and
  the `_run_one_manager` call site inside `_run` (timed around the call,
  not inside `_run_one_manager` itself, since that function already
  swallows its own exception into `None` — the timer has to live where
  the failure is *handled*, not where it's caught, or a failed run's
  duration never reaches the print/log that reports it). Both `finally`
  blocks, so a run that dies partway still reports how long it ran.
  **Deliberately not** in `ServerInventoryProvider.collect()` (printing
  from `app.domain` is a layering violation domain must not know about
  stdout) and **not** a decorator (`collect()` is an async generator
  whose `GeneratorExit`/`aclosing` behavior is load-bearing — see
  `provider.py:224`, `run_collector.py:502` — and a decorator around an
  async generator changes when that fires).
- Appended to both existing stdout summary lines: real run gets
  `took=2m 13s`, dry run gets `(took 42.3s)`. Verified manually end to
  end (a slow fake provider + a fake failing `_run_one_manager`) — the
  FAILED path was the one worth checking by hand, since it's easy to
  wire the happy path and miss it:
  `manager=ucs-central FAILED (see logs) took=0.0s`.
- One structured event, `collector.run_complete`, from both paths, with
  the duration as a raw `seconds` float (never the formatted string —
  the point is to graph it) and `dry_run=True/False` so a dashboard can
  filter dry runs out. `manager_type` is not logged explicitly; it
  arrives via the contextvars binding `_run` already does, confirmed in
  the manual run's actual log line (`manager_type=UCS_CENTRAL` present
  with no code added for it).
- `intersight.run_summary`'s `seconds=` deleted — exactly one duration
  per run, from one place, in one format, now.
- **No Prometheus metric.** A CronJob pod is never scraped (item 0 of
  CLAUDE.md's not-done list — staleness detection is still open), so a
  metric emitted from a collector process reaches nothing. The log field
  is the export path; a future dashboard reads it from the log pipeline,
  not from a metric with no scraper.

Tests: `_format_duration` at 0.4s/59.9s/60s/133.4s (the 60s boundary is
the whole logic); the real path logging `collector.run_complete` with a
`float` `seconds` and `dry_run is False`; the existing total-failure exit
test extended to assert `took=` is present in that path's output too, per
above. No test asserts an exact duration. Full gate green: `ruff check`,
`ruff format --check`, `ty check`, `pytest -q` (1080 passed, up from
1075 — 5 new).

---

## Phase 9 — Test gaps

**Shipped as `test: skip cleanly instead of erroring when the dev stack is
down`.**

- **T1.** `tests/api/` had no conftest and no skip guard —
  `tests/integration/`'s memoized-unreachable mechanism was applied to
  that directory only. Re-measured before fixing: with Mongo on a dead
  port, `tests/api/test_health.py` gave 3 errors in 21.23s where
  `tests/integration/` gave 5 clean skips, confirming the finding was
  still real. Fixed by lifting the memo out of
  `tests/integration/conftest.py` into a new shared module,
  `tests/_stack_availability.py` (`connect_mongo_or_skip`/
  `connect_redis_or_skip`, deliberately not `test_`-prefixed so pytest
  doesn't collect it as a suite of its own), and adding
  `tests/api/conftest.py` with one `autouse=True` fixture that calls both
  before any test's own `app_context` fixture runs — rather than editing
  each of the eight near-identical `app_context` fixtures individually
  (that duplication is S4, out of this phase's scope). Verified: a dead
  Mongo port now gives `4 skipped in 5.49s` for `tests/api/test_health.py`
  (was 3 errors in 21.23s); the real stack still gives `55 passed in
  5.26s` for the whole directory. `tests/integration/`'s own behaviour is
  unchanged (`75 passed` live, `10 passed, 65 skipped` dead — some of that
  directory's tests need neither service). Picked up S11 in the same
  move since it's a one-word fix to code already being touched: the
  shared helper catches `(PyMongoError, OSError)`, not `PyMongoError`
  alone, so a malformed URI scheme memoizes instead of propagating past
  the guard it was supposed to hit.
- **T3 + C4 — four untested behaviours, all in `tools/run_collector.py`
  or the RFC 9457 envelope, each verified to fail before its test existed
  and pass after (confirmed by breaking the source under `git stash` and
  rerunning, then restoring):**
  - `_UNFILTERED_TYPES` (`REDFISH_STANDALONE`'s exemption from
    `INVENTORY_COLLECTOR_NAME_PATTERN`) and `_ENDPOINTLESS_TYPES` (its
    `ManagerConnection` built from `settings.redfish_inventory_file`
    rather than resolved) — this is C4, restated as T3's first half.
    Neither string appeared anywhere in `tests/` before this. New class
    `TestEndpointlessAndUnfilteredTypes` in `tests/unit/tools/
    test_run_collector.py`: one test asserts `_run(manager_type=
    REDFISH_STANDALONE)` never calls `EnvConnectionResolver.resolve`
    (a resolver stub raises if it's called at all) and that the printed
    manager endpoint is the inventory file path; the other asserts a
    configured `^ocp` pattern does not filter a standalone run's output.
    Emptying both frozensets by hand reproduced exactly the failure each
    test names — `ManagerNotConfiguredError` demanding
    `INVENTORY_REDFISH_STANDALONE_IP` for the first, a silently dropped
    server for the second — confirming this was a real, not
    theoretical, gap.
  - `--dry-run` exit codes (10b): nothing previously drove `_run(dry_run=
    True)` to its returned code, only its printed output/count. New
    `TestDryRunExitCodes`: success returns 0 (already known from other
    tests but not from this angle) and a raised exception from
    `_dry_run_one_manager` returns 1 with `FAILED` printed — the one
    branch (`_run:1049-1052`) with no prior coverage.
  - RFC 9457 `type`/`title`/`detail` (11): confirmed the audit's own
    claim by temporarily deleting the `"title"` key from
    `_problem_response` and rerunning — the suite stayed green. Extended
    `tests/api/test_servers.py::test_unknown_filter_returns_400_problem_json`
    (the one existing test already asserting the envelope shape) with
    `type == "/problems/unknown-filter"`, `title == "Unknown Filter"`,
    `detail == "Unknown filter: 'not_a_real_filter'"` — re-ran the same
    deletion afterward and confirmed it now fails.
- **`ty` already covers `tests/`** — Phase 1's `686160f` did this
  (`ty check backend/app tools tests`), so nothing further was needed
  here; `uv run ty check backend/app tools tests` passes clean including
  every file this phase touched.

Full gate green: `ruff check .`, `ruff format --check .`, `ty check
backend/app tools tests`, `pytest -q` — **1084 passed**, up from 1080 (4
new in `test_run_collector.py`; the RFC 9457 fix extends an existing test
rather than adding one).

---

## Phase 10 — Docstrings and comment pruning

**Commit:** `refactor: give every backend function a Google-style docstring`

The largest and least risky change, deliberately last.

**Scale, measured — larger than the brief assumes:** 244 of 363 items in
domain/API/application have **no docstring at all** (67%); 39 are
older-style; 4 files fully conform. `tools/` is 42 of 84. All seven
provider packages already conform.

**The trap:** ruff's shipped `convention = "google"` fires `D212`
**319 times inside the files `CLAUDE.md` says are already converted**,
because convention 8's own example puts the summary on the line after
`"""`. Select `D213`, disable `D212` — otherwise the sweep rewrites the
house style into something else. Enable `D` as a gate **in this commit**,
not in Phase 8, or CI goes red between the two.

Two decisions to make explicit rather than let a sweep make silently:
every Pydantic model class currently uses field comments instead of a
class docstring, and all 24 `AppError` subclasses have none.

Prune comment walls in the same pass. **Never delete a hard-won fact** —
move it to `docs/` with provenance intact. Flagged as move-not-delete:
the `INVENTORY_GPU_MODELS` field-name bug, OneView's `count=-1` meaning
64, the UCS-login-without-endpoint rationale, the Dell two-login
exception, and `ProfileTemplate`'s four-vendor comparison.

**Docstring length, decided 2026-09-07 (user directive, applies to this
sweep and to every function written after it):** the summary before
`Args:`/`Returns:`/`Raises:` is **1-3 lines**, not more, unless the
function genuinely needs more to avoid a real misuse — most functions
already say what they do in their name and signature, and a long prose
summary on top of that is exactly the "wall of prose between statements"
convention 8 was written to stop. `Args:`/`Returns:`/`Raises:` stay full
and typed regardless of summary length — this rule is about the prose
above them, not about dropping the structured part. A summary that
already fits 1-3 lines needs no change in this sweep.

**Shipped as `refactor: give every backend function a Google-style docstring`.**

**Re-measured rather than trusted, per this session's own established
practice**: `uv run ruff check --select D backend/app tools` (before any
`[tool.ruff.lint]` config existed to scope it) found **402** real
violations, not the stale 244/363 + 42/84 estimate above — the original
audit's numbers were from an AST pass with its own conformance
definition, not from ruff's actual `D` rule set. `git diff --stat` at
completion: **127 files, ~2,700 insertions**.

- **The trap was real but the fix wasn't "select D213"** — D213 doesn't
  need separate selecting; `convention = "google"` enables D212 by
  default, and simply adding `"D212"` to `[tool.ruff.lint] ignore`
  (alongside `D203`, which the CLI already warns is incompatible with
  `D211`) is sufficient. Confirmed empirically: CLAUDE.md's own docstring
  example (`"""` alone, summary on the next line) fails with `D212` under
  bare `select = ["D"]` + `convention = "google"`, and passes clean once
  `D212` is ignored — no `D213` selection needed or possible (it's the
  mutually-exclusive counterpart, not an independent switch). `D` was
  added to `[tool.ruff.lint] select` and `tests/**` was added to
  `per-file-ignores` for `D` in the same commit as the docstring sweep
  itself, exactly as the trap paragraph above demanded (not staged into
  Phase 8, which would have gone red for however long the sweep took).
- **A second, sharper trap found only by hitting it twice**: `D205`
  ("blank line required between summary and description") rejects a
  summary that wraps naturally across 2-3 physical lines with *no* blank
  line — even one grammatical sentence, even well inside the 1-3 line
  cap above. pydocstyle treats only the literal first physical line as
  the summary; anything on the next line without an intervening blank is
  "description" and must be separated from it. This is not mentioned
  anywhere in pydocstyle's own docs in those terms and was the single
  highest-frequency violation (136 of 402). Every parallel worker below
  was briefed on it explicitly, with a worked example, after the
  coordinating session's own first two docstrings on `errors.py` hit it.
- **A related trap, `ruff format` vs `D210`**: a docstring whose content
  starts with a literal `"` character (`""" "REVISION_CONFLICT" -> ...`)
  gets a space inserted by `ruff format` to disambiguate the delimiter,
  which then fails `D210` ("no whitespace after opening quotes") —
  `ruff format` and `ruff check` disagree and cannot both be satisfied by
  quoting differently; the only fix is not opening a docstring with a
  literal quote character.
- **The two decisions, made and applied**: every Pydantic `BaseModel`
  subclass got a short (1-3 line) class docstring *additively* — every
  existing per-field `#` comment was left untouched, verified by grep
  against every fact named in convention 8's own list (GPU catalog
  equality rule, OneView `count=-1`, the UCS-login-without-endpoint
  rationale, the Dell two-login split, Intersight's `TotalMemory` unit
  caveat, `ProfileTemplate`'s four-vendor comparison — all confirmed
  present in the final tree). All 24 `AppError` subclasses got a
  one-line docstring naming their HTTP status and condition;
  `ManagerHasChildrenError`/`InvalidManagerHierarchyError` — dead code,
  never raised anywhere, a leftover from before managers became
  config-derived projections — are documented as such rather than given
  a docstring implying they fire from somewhere.
- **Execution: seven parallel `general-purpose` agents plus the
  coordinating session's own work**, split by directory
  (`infrastructure/mongodb`+`redis`+`logging`; `domain/models`;
  `domain/services`+`ports`+`value_objects`; `api/v1`+`middleware`;
  `application/services`+`config`; `tools/`; `infrastructure/providers`+
  `utils`+`observability`), each with the full docstring-shape briefing,
  the two D-traps above, the Pydantic/AppError decisions, and an explicit
  "never delete a hard-won fact" instruction. `backend/app`'s top-level
  files (`main.py`, `dependencies.py`, `errors.py`,
  `exception_handlers.py`) and `infrastructure`'s top-level/`credentials`
  files were done directly by the coordinating session rather than
  delegated, being small and foundational. One directory,
  `backend/app/domain/enums/` (a package, not the single file the
  original file-listing command assumed), was missed by every worker's
  scope and caught only by the final repo-wide `ruff check .` — fixed
  directly afterward. Two mechanical defects survived a worker's own
  verification and were caught only by the coordinating session reading
  the diff: `tools/verify_ucs_central.py` and `tools/verify_intersight.py`
  each ended up with a module docstring containing the original opening
  sentence duplicated/split awkwardly around a synthetically-added
  one-line summary (the worker's own account of *why* — reconciling a
  sub-100-char single-line-summary requirement against an original
  opening sentence too long to fit one line — was accurate; the specific
  wording just needed tightening) — both rewritten to one clean summary
  line each, re-verified.
- **Verified**: `uv run ruff check .`, `uv run ruff format --check .`,
  and `uv run ty check backend/app tools tests` all clean; `uv run
  pytest -q` — **1084 passed**, unchanged from Phase 9 (documentation-only
  change, no test was expected to move).

---

## Phase 11 — Documentation truth pass

**Shipped as `docs: correct 18 statements that describe a shape this code no longer has`.**

The full 18-item table lives in `docs/notes/2026-09-audit-deploy-ci-docs.md`
§4, not `2026-09-audit.md` §4 item 6 (which only summarizes it as "18
false statements" with five examples) — the plan's own "audit §4.6"
pointer sent the wrong direction, worth recording here so the next
session doesn't hunt for a §4.6 that doesn't exist in the file it names.

**Re-verified each of the 18 against the current tree before touching
anything, per this session's own established practice** ("re-measure
rather than trust"): 4 of the 18 were already resolved by later phases
and needed no change — `pip-audit` is clean (Phase 3's `cryptography`
bump), `classification_rules.py`/`health_policies.py` module docstrings
were already fixed by Phase 10, and
`docs/test-redfish-standalone-collector.md`'s flagged `site_id=one`
example no longer exists in the file at all. The remaining 14 were fixed:

- `docs/architecture.md` — the ~40-line Slice 5 admin-UI/`ConditionBuilder`/
  `ShadowPanel` writeup rewritten to describe the real read-only merged
  rules/policies page, framed explicitly as "this subsystem was
  subsequently removed" rather than silently swapped; "334 backend
  tests" → 1084 (re-measured via `pytest --collect-only`); dropped
  "classification rule CRUD, health policy CRUD" from what
  `AuditService.record()` covers.
- `docs/arc42.md` — Source-filter claim (offers 5, not 3, verified in
  `frontend/src/api/sites.ts`), ADR count (18 → 23, and the ADR-0023 row
  itself was missing from the index table — added), Q6's "reaches …
  editors" (dropped), the stale "arc42 corrects CLAUDE.md" note (both
  documents already agree since convention 6 was corrected), and the
  risk register's Source-filter/Dell-seeding entries (both resolved —
  `SOURCE_PROVIDERS` has all five, `OPENMANAGE` is in the generator's
  `COLLECTOR_TYPES`) replaced with what's actually still a risk (a future
  hand-maintained list could drift the same way) plus a note that the
  docstring convention is done for backend/tools but not the frontend.
- `README.md` — "admin UIs" → the real read-only-merged-page description;
  the ASCII diagram's "classification/health-policy editors" → "read-only
  rules/policies page"; the self-contradiction between "eventually Dell
  OpenManage/HPE OneView" (line 58) and "five collectors exist today"
  (line 152) resolved in favor of the true state; the Source-filter/Dell
  seeding paragraph rewritten to name what the generator's `collector_for`
  actually does (Dell → `OPENMANAGE`, not folded into
  `REDFISH_STANDALONE`) rather than describing a gap that closed. Found
  and fixed one item outside the original 18 while re-verifying this
  section: `uv run ruff check --select D .` was described as "not a CI
  gate" — Phase 10 made `D` part of the main `ruff check .` gate, so that
  line was already stale from this pass's own earlier work.
- `docs/diagrams/runtime-architecture.architecture.json` +
  `runtime-architecture.html` — added `openmanage`/`oneview` components
  via the `archify` skill rather than hand-editing 14k lines of generated
  SVG. Non-trivial: fitting a 5-source fan-in into the existing 3-source
  layout needed the `collectors` box grown from 66px to 140px tall (more
  room for the five incoming ports) and alternating `labelDx`/`labelDy`
  offsets on all five vendor-read connections to clear
  `composition/label-route-clearance` — iterated against
  `archify validate` until showcase profile passed clean (9/9 checks, 0
  errors, 0 warnings), then `deliver`, with source evidence verified
  against this repo. `visual-check` (real-browser evidence) could not run
  — no Chrome/Chromium available in this environment — so the deterministic
  delivery receipt is the only verification this diagram got; a human or
  image-capable review is still owed before calling the diagram's
  *appearance*, not just its structural correctness, settled.
- `CLAUDE.md:295` and `docs/arc42.md`'s own copy of the same sentence —
  `_PROVIDER_FACTORIES` → `PROVIDER_FACTORIES` (the symbol is public, no
  leading underscore; CLAUDE.md already used the correct name elsewhere
  in the same file, which is how the audit caught it). The many
  `_PROVIDER_FACTORIES` mentions inside `docs/adr/*.md` were **left
  alone** — ADRs are point-in-time historical records in this project's
  convention, not living documents, and at least one of them may predate
  whatever later renamed the symbol; only the two currently-living
  documents were corrected.
- `scripts/dev-up.sh` — the header's false "no `docker-compose` plugin
  installed" claim (CLAUDE.md's own 2026-09-05 measurement found Docker
  Compose v5.5.1 present and `docker compose` the *preferred* path) and
  its citation of the 75-section chat spec (convention 1 explicitly
  forbids "the spec says so" as a justification) both rewritten to state
  the script's real reason for existing: it needs no compose provider at
  all, which is what the air-gapped and CI paths actually depend on.
- `docs/adr/0007-scale-verification-and-request-coalescing.md` — a dated
  addendum (2026-09-06/07), not a rewrite, recording
  `docs/notes/2026-09-research-performance.md` §7.3's re-measurement:
  the named mechanism (MongoDB flips between an `IXSCAN[search_tokens]`
  plan and an `IXSCAN[name_id]`-plus-`FETCH`-filter plan depending on
  scale) is confirmed at 50k, but the specific 700-800ms zero-match p99
  does not reproduce under `--seed 42` — the zero-match case is the
  *cheapest* query in the set at both 10k and 50k now, because the
  planner already picks `search_tokens` for it. Attributed to a
  materially different seed distribution (1,570 servers behind the
  measured low-selectivity term, 3% of the fleet, not "roughly a
  quarter"), not a wrong original measurement — the coalescing decision
  this ADR is actually about is unaffected either way.

Documentation-only change; no `ruff`/`ty`/`pytest` gate applies. Verified
by re-grepping every corrected claim after editing to confirm zero
remaining stale hits (a couple of `git grep`s per item), and by the
`archify validate`/`deliver` receipts for the diagram.

---

## Phase 12 — One login per collection run (deferred, not scheduled in this pass)

**Not part of this refactor.** Raised during Phase 1 review, kept out of
it deliberately: it changes real vendor session-lifetime behaviour rather
than restructuring an interface, and a session-lifetime bug against a
manager that enforces a per-user session cap is exactly the class of
defect that has only ever surfaced on live hardware (ADR-0009's whole
history). Bundling it into Phase 1 would mean that if something breaks,
there'd be no way to tell whether the contract change or the login change
caused it. It gets its own phase, its own ADR, and — like Intersight and
OneView — a live-hardware confirmation before it ships, via
`docs/field-test-checklist.md`.

**The defect.** `IngestService.ingest` (`ingest.py:308-310`) is the one
path every real collection run takes, and it always does this:

```python
await provider.health_check()          # a full login, then an immediate logout
async for provider_server in provider.list_servers():   # a second, independent login
```

For `ucs_manager` (`provider.py:85-96` then `:119-193`) and `ucs_central`
(`:280-293` then `:312-316`), each login is, per the collector's own
comment at `run_collector.py:883`, "~4 sequential HTTP round trips (auth,
then the SDK's own is-this-UCSM / version / domain-name probes)" — so
every production run against UCS pays roughly 8 round trips of login
overhead where 4 would do, and opens a second session against UCS
Manager's per-user session cap for no reason. `intersight`
(`provider.py:298-300`) pays the same shape at smaller cost (build a
client, call its own `.health_check()`, `aclose()` it, then build a
second client for `list_servers()`).

**This has already been half-noticed, in the wrong place.** The comment
at `run_collector.py:883` exists because someone saw this exact waste —
but only in the CLI's own call site, where `provider.health_check()` is
now deliberately *not* called a second time before `ingest_service.ingest()`,
specifically to avoid **tripling** the login cost. That fix does nothing
for the doubling that happens inside `ingest()` itself on every run; there
is no workaround for it today.

**Why it isn't simply a bug to fix inline.** `health_check` and
`list_servers` are two different interface members precisely because
"can I log in" needs a fast, isolated answer *before* a run commits to
the expensive work — for most systems that check is cheap. For UCS and
Intersight, logging in **is** the expensive step, so the check ends up
costing nearly as much as the thing it's guarding. Fixing this means one
login shared across both calls, which means both calls need to agree on
when that login happens and who owns closing it — exactly the kind of
session-lifetime restructuring Phase 1 explicitly avoided doing to any
vendor's mapping logic.

**What changes, roughly:** `ucs_manager`, `ucs_central` and `intersight`
each hold one client for the duration of a call to `collect()` (the
Phase 1 method), login once inside it, and make their own
`health_check()` a no-op once already connected — or, more precisely,
`health_check` and the first phase of `collect()` merge into one login,
with `list_servers`/`_list_servers` reusing the already-authenticated
client. `openmanage` and `oneview` are unaffected: their client already
*is* the context manager (`openmanage/provider.py:162,213`;
`oneview/provider.py:166,185`), so `health_check`'s brief open-and-close
is already close to free. `redfish` is unaffected — it never had a
single login to begin with (`redfish/provider.py:193`).

**Verified by:** a regression test asserting `client.login()` is called
exactly once per `collect()` invocation, for each affected provider —
today nothing asserts the call count, which is exactly how the doubling
went unnoticed. Then, per `docs/field-test-checklist.md`, a live run
against UCS Central and UCS Manager (the same trip already queued to
settle the `total_memory` unit) confirming session count and behaviour
under the real per-user session cap, before this ships.

---

## What this plan deliberately does not do

- **Touch authentication.** `get_current_actor` stays as it is.
- **Re-litigate ADR-0022.** No Redfish pass for HPE, no BMC credentials,
  no `INVENTORY_ONEVIEW_BMC_*`.
- **Weaken a collector to tidy it.** Redfish's deliberate no-retry-on-5xx
  (BMC account lockout) and its shielded session `DELETE` stay. The
  Intersight `verify=False` default stays as the documented decision it
  is; the proposal is only to *add* `INVENTORY_INTERSIGHT_CA_BUNDLE`
  alongside it.
- **Rewrite vendor mappings.** Phase 1 restructures the contract around
  them.
- **Merge `health_check` and `list_servers` into one login.** Real,
  measured waste (see Phase 12) — kept out of this pass because it
  changes vendor session lifetime rather than an interface, and needs
  live-hardware confirmation this pass has no way to get.
- **Add project-authored decorators.** Researched honestly, verdict is
  zero: retry policies are deliberately opposite between collectors,
  timing is already one middleware (and `prometheus_client` ships
  `Histogram.time()`), cache-aside call sites differ structurally, and
  nothing in the project is deprecated. The real retry gap is that
  **OneView and OpenManage have no retry at all** — fixed in their
  clients in Phase 2, not with a decorator.
- **Migrate off `httpx`.** Confirmed correct: `requests` has no async API
  and only a `(connect, read)` timeout pair. Worth recording that
  `httpx`'s `retries=` reaches only connection setup — it never retries a
  read timeout, a 429 or a 503.

---

## Picking this up in a new session (2026-09-07, after Phase 9)

Handed off deliberately clean — verified before ending this session, not
assumed:

- Phases 1-9 are done, committed on `dev-refactor`, and (once this
  session pushes) match `origin/dev-refactor`. No stash, no open
  worktree besides the main one. **Phase 10 next** — the largest and
  least risky phase, deliberately last; read its own section above
  (docstring scale, the `D212`/`D213` trap, the length rule) before
  starting rather than working from memory of it.
- The dev stack was already running when this session began (measured
  22h uptime) — left as found; a future session should still check
  `podman ps` before assuming anything about it.
- The only untracked files are `.claude/.proven-config-version` and
  `.claude/proven-config.json` — pre-existing debris from some other
  tool's own state-caching (not from this refactor, not written by any
  work in this plan), present since before Phase 6. Not a decision to
  make on this plan's behalf; leave them alone unless the user asks.
- The standing gate before calling any phase done is still: `uv run
  ruff check . && uv run ruff format --check . && uv run ty check
  backend/app tools tests && uv run pytest -q`, plus the frontend
  equivalent for any frontend change, plus a real Playwright run if the
  change touches anything E2E covers. This WSL environment has a few
  gotchas that are not this repo's problem but will look like one if
  re-discovered from scratch: `podman ps` before assuming a stuck
  command is a regression (a timed-out command can reap the dev stack's
  containers); Playwright's chromium needs `LD_LIBRARY_PATH` pointed at
  a hand-extracted lib bundle to launch at all in this sandbox. Claude
  Code's own persistent memory for this project already has both, in
  more detail, if the assistant picking this up is Claude.
