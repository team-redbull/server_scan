# Production-hardening plan — 2026-09-06

Companion to `docs/notes/2026-09-audit.md` (findings, with IDs referenced
here) and the seven `docs/notes/2026-09-research-*.md` files.

**Status: approved 2026-09-06. Phases 1-6 done, committed, and pushed to
`dev-refactor` (`686160f`, `453f47e`+`8dfed16`+`517cfce`, `c90968a`,
`4806d21`+`6066cc5`+`2920510`, `b5d6702`, `37d1cce` respectively — Phase 2
shipped as three commits and Phase 4 as three instead of one, see their
own sections for why). Phase 6 also surfaced and fixed an unrelated dev-
tooling bug (`d448822`): `scripts/dev-up.sh down` never removed Mongo's
named volume, so `down && up` silently kept the previous run's data
instead of the empty database the README documents that sequence as
producing. Phase 7 next.**

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

**Commit:** `ci: enforce the layered architecture with import-linter`

- **M4 first** — add `backend/app/infrastructure/__init__.py`. import-linter
  cannot see the package until this exists.
- `import-linter`, gating **the 4 contracts that already hold**. The 5th
  (application never names a concrete adapter) is broken 8 ways — all
  constructor params typed to Mongo repositories, with `ServerRepository`
  already a Protocol that is bypassed and three repos having no port at
  all. Record it as known debt with a `ponytail:` marker rather than
  gating a red contract or doing a speculative ports sweep here.
- `ruff --extend-select PTH,LOG,RET,PERF,FURB,TRY004,TRY300,TRY400,C901`
  — 27 findings, all true positives, 3 autofixable. `TRY400` is losing
  tracebacks in two collectors.
- `deptry --known-first-party app` (5 findings).
- Make `oxlint` actually gate — it currently exits 0 with a
  `no-floating-promises` warning present.
- Bump `ty` 0.0.76 → 0.0.78 (measured clean) and
  `docker/setup-buildx-action` v4.2.0 → v4.3.0 (resolves).
- **Documented local commands, not gates:** `vulture`, `knip`, ruff `D`.
- **Explicitly not adding:** `bandit` (all 12 findings duplicate
  already-selected ruff `S` codes carrying reasoned `# noqa`; its only
  unique output is 2 false positives), `radon`/`xenon` (maintainability
  index is flat-A across every file *because* MI rewards comment ratio and
  this repo is comment-heavy by policy — the metric does not measure what
  we care about), `deadcode`, bundle-size tooling (140 kB gzip against
  Vite's own 500 kB warning).
- `ruff` 0.14.0 → 0.16.6 passes `check` but **reformats 6 files** — its
  own commit, or deferred.

---

## Phase 9 — Test gaps

**Commit:** `test: skip cleanly instead of erroring when the dev stack is down`

T1 (`tests/api/` conftest + skip guard — the "hung suite" fix reached
`tests/integration/` only; measured 3 errors in 21.23 s versus
integration's 5 clean skips), T3 (`_UNFILTERED_TYPES`,
`_ENDPOINTLESS_TYPES`, `--dry-run` exit codes, RFC 9457
`type`/`title`/`detail` — dropping `title` currently passes the suite),
C4. Extend `ty` to `tests/` if Phase 1 did not.

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

---

## Phase 11 — Documentation truth pass

**Commit:** `docs: correct 18 statements that describe a shape this code no longer has`

The 18 false statements (audit §4.6), the `CLAUDE.md` corrections
(§4.1–§4.5), and an ADR-0007 update recording that its named mechanism
still holds at 50k while its numbers do not reproduce under the current
seed distribution — **an ADR update, not a code change**.

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
