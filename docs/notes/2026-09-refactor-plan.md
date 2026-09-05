# Production-hardening plan — 2026-09-06

Companion to `docs/notes/2026-09-audit.md` (findings, with IDs referenced
here) and the seven `docs/notes/2026-09-research-*.md` files.

**Status: awaiting approval. No source code has been changed.**

Ordering follows the brief: contract and architecture first while the diff
is still legible, mechanical sweeps last. One phase = one reviewable
commit. Every phase runs the full convention-7 gate before it is called
done, and the output is pasted into the report.

Three phases are **blocked on a decision** and are marked so: Phase 1
(Q6), Phase 4 (Q1, Q4), Phase 7 (Q7).

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

## Phase 1 — The provider contract  ⚠ blocked on Q6

**Commit:** `feat!: require every collector to report collection_errors and release its session`

### The decision you need to make first

The brief asks for an `ABC`. The research recommends a **6-member
`Protocol`** instead, and I think the evidence is good enough to put to
you rather than quietly follow either way.

| | ABC | 6-member Protocol |
|---|---|---|
| Catches a missing member | runtime `TypeError` | **ty, at the call site, by name** |
| Catches abstract instantiation | runtime | ty 0.0.76 **does not flag it** (probed) |
| Shared behaviour to inherit | ~none — providers differ in kind | n/a |
| Enforces `async with` | yes | **yes, twice** — protocol member *and* `invalid-context-manager` on the concrete type |
| Existing `@property` implementers | fine | fine, **if** declared a read-only property |
| Default method bodies | natural | PEP 544: a defaulted member becomes *required*; broke conforming classes in probe |

Proposed contract, verified `All checks passed!` under this repo's own
ty 0.0.76 against all three real shapes simultaneously — property-style
(`ucs_central`, `intersight`, `openmanage`, `redfish`), attribute-style
(`fake`, `ucs_manager`, test stubs), and the `_NameFilteredProvider`
wrapper:

```
provider_type: str
__aenter__          # connect / authenticate
__aexit__           # disconnect / logout — guaranteed
health_check
list_servers        # MUST stay `def`, not `async def`
collection_errors   # MUST be a read-only property
```

**Say the word and I build an ABC instead.** The brief's actual goal —
one file, named members, type-checked, obvious what a new vendor writes —
is met either way; only the mechanism differs.

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
(2 call sites, `:874` and `:693`), the seeder, and ~10 test stubs. The
stubs are **outside** `ty check backend/app tools`, so they are checked by
nobody today and carry coded `# type: ignore[...]` that ty ignores anyway
— extending ty's scope to `tests/` is the cheap guard and belongs here.

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
C8 (UCS call deadline + per-domain budget — **check Q3 first**),
C9 (OpenManage errors lost on early close), C10 (OneView bare `gather`),
M11 (pointless `async`).

**Could break:** C6 and C10 change streaming shapes; C8 changes how a
wedged domain terminates. Each needs a test that fails before the fix.

**Verified by:** the agent's repros, converted into regression tests, plus
the full gate.

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

## Phase 4 — API and domain correctness  ⚠ blocked on Q1, Q4

**Commit:** `fix: re-sync shipped health policies whose conditions changed in code`

C11 (bootstrap re-sync), C12 (nothing validates stored conditions since
writes were removed — **needs Q1**), C13 (optimistic concurrency —
**needs Q4**), M6/M7 (`mongo.ping_failed` gains `error=`/`exc_info`, and
`mongo_ping_failures_total` gets incremented), M8 (bind run context in
`run_collector.py` so `ingest.completed` carries `manager_type`), M9,
M10, M3 (delete dead `get_inventory`), M1 (dead read-only-migration code
— **needs Q1**).

---

## Phase 5 — Performance, measured only

**Commit:** `perf: stop re-reading classification rules and health policies once per server`

P1 (~20,000 collection reads per 10k run → load once per run), P2 (async
dependency functions, +0.42 ms/request measured; return cached bytes
without the decode/re-validate/re-encode round trip, 0.919 ms).

**The rule for this phase:** every change carries a before/after number in
the commit body, from the method in
`2026-09-research-performance.md`. Anything that does not move a
measurement does not get made. Explicitly **not** doing: `ORJSONResponse`
(measured as a regression — it disables FastAPI 0.141.1's `dump_json`
fast path) and any middleware change (+0.07 ms; the profiler's 79%
cumulative is an artefact).

---

## Phase 6 — Frontend correctness

**Commit:** `fix: stop showing "all healthy" for a site whose servers were never evaluated`

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

## Phase 7 — Operator UX  ⚠ blocked on Q7, build only what you approve

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

---

## Phase 11 — Documentation truth pass

**Commit:** `docs: correct 18 statements that describe a shape this code no longer has`

The 18 false statements (audit §4.6), the `CLAUDE.md` corrections
(§4.1–§4.5), and an ADR-0007 update recording that its named mechanism
still holds at 50k while its numbers do not reproduce under the current
seed distribution — **an ADR update, not a code change**.

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
