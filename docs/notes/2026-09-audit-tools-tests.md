# Audit: `tools/` and `tests/` — production-hardening pass, 2026-09-06

Read-only audit. **No source file was changed.** Scope: `tools/` (8 files,
3,348 lines) and `tests/` (74 files, 16,397 lines, 1,037 collected tests).

Everything below was read or measured, not inferred. Where a claim is
empirical the command that produced it is named.

---

## 1. `tools/run_collector.py` — the map

1,051 lines. The Kubernetes `CronJob` entry point: one CronJob per manager
type, `uv run python -m tools.run_collector --manager-type <TYPE>`.

### Control flow

```
main(argv)                                    :1032
  ├─ _parse_args(argv)                        :387
  ├─ --debug-xml  -> os.environ["INVENTORY_UCS_DUMP_XML"] = "1"
  ├─ --debug-http -> os.environ["INVENTORY_REDFISH_DEBUG_HTTP"] = "1"
  └─ raise SystemExit(asyncio.run(_run(...)))

_run(manager_type, dry_run, limit)            :903
  ├─ get_settings(); configure_logging()
  ├─ MongoClientHolder.connect()              <-- unconditional, dry run included
  ├─ connection = ManagerConnection(endpoint=<inventory file>)  if endpointless
  │              else EnvConnectionResolver.resolve(type)
  ├─ if UCS_CENTRAL: resolve_login(UCS_MANAGER)   (pre-flight for the 2nd login)
  ├─ except ManagerNotConfiguredError -> print(exc); return 2
  ├─ manager = manager_for(type, connection)  :432
  ├─ name_pattern = "" if type in _UNFILTERED_TYPES else settings.collector_name_pattern
  ├─ if dry_run:  _dry_run_one_manager(...)   :675   -> 0, or 1 on any exception
  └─ else:
       ensure_indexes -> build IngestService  :970-999
       outcome = _run_one_manager(...)        :864
       outcome is None                        -> 1
       collection_errors or summary.errors    -> 3
       otherwise                              -> 0
```

### `PROVIDER_FACTORIES` (`:388`, public)

The single source of truth for "which collectors exist". Five entries:

| `ManagerType` | Factory | Endpointless | Name-filter exempt |
|---|---|---|---|
| `UCS_CENTRAL` | `_ucs_central_provider` :171 | no | no |
| `OPENMANAGE` | `_openmanage_provider` :92 | no | no |
| `REDFISH_STANDALONE` | `_redfish_provider` :229 | **yes** | **yes** |
| `INTERSIGHT` | `_intersight_provider` :267 | no | no |
| `ONEVIEW` | `_oneview_provider` :302 | no | no |

`UCS_MANAGER` is deliberately absent — it is reached per domain by
`UCS_CENTRAL`. Two other consumers import this dict rather than restating
it (`tests/unit/test_frontend_manager_types.py:41`,
`tests/unit/infrastructure/providers/test_generator.py:69`); see §4.

`_ENDPOINTLESS_TYPES` (`:355`) and `_UNFILTERED_TYPES` (`:363`) are two
separate `frozenset`s with identical contents (`{REDFISH_STANDALONE}`) and
different reasons, each documented. Correct as written.

### `_NameFilteredProvider` (`:457`) and the `collection_errors_of` smell (`:517`)

`_filtered(provider, pattern)` (`:513`) wraps in `_NameFilteredProvider`
when `pattern` is truthy and returns the provider *unwrapped* when it is
empty. The wrapper re-exposes exactly three members of the thing it wraps:

- `self.provider_type = inner.provider_type` (copied in `__init__`)
- `collection_errors` (a property delegating through `collection_errors_of`)
- `health_check` / `list_servers` (the two Protocol methods)

**What `collection_errors_of` papers over.** `ServerInventoryProvider`
(`backend/app/domain/ports/provider.py`) is a `Protocol` declaring
`health_check` and `list_servers` and nothing else. `collection_errors` is
therefore *not* part of the contract: only a fan-out collector (UCS
Central over domains, Redfish over BMCs, OneView over PSUs) can partially
fail, so three of five providers have the attribute and the fake seeder
does not. `collection_errors_of` reads it with `getattr(provider,
"collection_errors", ())` and takes `object` as its parameter type,
discarding static typing entirely at the one place a partial run is
detected.

The consequence is not hypothetical. It is a *structural* gap, and it has
two faces:

1. **The type checker cannot help.** A provider that renames the attribute,
   or returns a `list[Exception]` instead of `tuple[str, ...]`, produces a
   silently complete-looking run and exit code 0. Nothing in the gate
   catches it — `ty check` sees `object`.
2. **The wrapper is a manual allowlist.** Any *future* out-of-band signal a
   provider grows (a "servers skipped" count, a partial-page marker) is
   silently swallowed the moment `INVENTORY_COLLECTOR_NAME_PATTERN` is set,
   because `_NameFilteredProvider` forwards only what someone remembered to
   add. That is the same class of drift the frontend/seeder guards were
   written to stop, and here there is no guard.

**This is the concrete motivation for the pass's headline item, a real
provider base class.** An ABC declaring `collection_errors` with a
`() -> tuple[str, ...]` default implementation deletes `collection_errors_of`
outright, makes the wrapper a subclass that inherits the forwarding, and
puts the attribute back inside the type checker's reach. Effort: M.

### `--dry-run` (`_dry_run_one_manager`, `:675`)

Bypasses `IngestService` entirely — not a no-op repository — so what is
printed is the raw `ProviderServer`, before classification, health
evaluation and correlation. The name filter is applied *inside* the
provider chain (`_filtered`), which is exactly why it must live in a
wrapper and not in `IngestService`: a filter there would make dry runs
print servers a real run would silently drop. Tested
(`test_dry_run_shows_only_what_a_real_run_would_write`, :718).

`_UNREAD = "not read"` (`:573`) distinguishes "the provider could not read
this" from `—` ("read it, there is none") — the print-side counterpart of
the `None`-vs-zero rule. Correct and tested.

### Exit codes — the documented contract

| Code | Meaning | Raised at | Tested |
|---|---|---|---|
| **0** | Complete run. Every server the provider reported was ingested, no collection errors, no ingest errors. Also the successful `--dry-run` result. | `:1028` (real run), `:967` (dry run) | yes — `test_a_complete_run_exits_zero` :820 |
| **1** | Total failure of this manager. `_run_one_manager` returned `None` (any exception during build/health-check/ingest), or `--dry-run` raised. Prints `manager=<name> FAILED (see logs)`; the real error is only in the structured log. | `:1005` (real run), `:966` (dry run) | yes — `test_a_total_failure_still_exits_one` :855 |
| **2** | Not configured. `ManagerNotConfiguredError` from `EnvConnectionResolver.resolve`, or from the `UCS_CENTRAL` second-login pre-flight. The exception message — which names the exact `INVENTORY_*` variables to set — is printed to stdout. | `:946` | yes — `test_missing_configuration_still_exits_two` :866 |
| **3** | **Partial run.** Servers were written, but this run did not see the whole fleet: `outcome.collection_errors` non-empty (an endpoint was unreachable) **or** `summary.errors > 0` (servers failed to ingest). Prints `PARTIAL` plus one line per failure. | `:1027` | yes — `test_an_unreachable_domain_exits_three` :828, `test_failed_ingests_alone_exit_three` :843 |

Exit 3 is the load-bearing one and its reasoning is right: reported as 0, a
bad credential on one of ten UCS domains is indistinguishable from a
healthy run against a smaller estate, and stays invisible for weeks. All
four codes are covered by `TestRunExitCodes` (`:794`).

**Collision worth knowing:** `argparse` exits **2** on a usage error
(unknown `--manager-type`, missing required arg), the same code as "not
configured". A CronJob's alerting cannot tell a typo in the manifest from
an unset Secret. Not a bug to fix silently — it is a documented-contract
question for the user (§7).

### Partially-configured vendor — verified, and it works

The brief asked whether `ManagerNotConfiguredError` names the missing
variables. It does, on all four paths, and each was read:

- `EnvConnectionResolver.resolve` builds `missing` from `_env_var(field)`
  and raises `"<TYPE> is not configured — set INVENTORY_A, INVENTORY_B."`
  (`backend/app/infrastructure/credentials/env.py:113`).
- `resolve_login` does the same for a login-only type (`:151`).
- `UCS_MANAGER` and `REDFISH_STANDALONE` get bespoke messages explaining
  that no endpoint variable exists *by design* and what to set instead
  (`env.py:91`, `:99`) — this is what stops the tool demanding
  `INVENTORY_UCS_MANAGER_IP`, a variable that deliberately does not exist.
- Dell's second login is checked in `_openmanage_provider` (`:106`) before
  any connection, naming `INVENTORY_OME_BMC_USERNAME`/`_PASSWORD` and the
  ADR. Tested at `test_openmanage_without_a_bmc_login_is_rejected_before_connecting`
  (:136).

This is done well and is worth preserving verbatim through any refactor.

---

## 2. `tools/` findings

Severity categories: **C**orrectness · **M**aintainability · **P**erformance
· **Co**nsistency.

| # | file:line | Finding | Cat | Fix | Eff |
|---|---|---|---|---|---|
| T1 | `run_collector.py:517` | `collection_errors_of` types its parameter `object` and reads the attribute by `getattr`, so the one signal that distinguishes a partial run from a complete one is outside the type checker and outside the `ServerInventoryProvider` Protocol. `_NameFilteredProvider` forwards it by hand. See §1 for the full argument. | C | Provider ABC declaring `collection_errors` with a `-> tuple[str, ...]` default; delete the helper; make the wrapper a subclass. | M |
| T2 | `run_collector.py:918` | `--dry-run` connects to MongoDB before doing anything, although the dry-run path uses no repository at all (`:960` comment says "no repositories at all"). A dry run against a vendor therefore fails when Mongo is down — the one situation an operator most wants to probe a vendor in. | C | Move `MongoClientHolder(...).connect()` below the `if dry_run:` block. | S |
| T3 | `run_collector.py:896` | `_run_one_manager` catches bare `Exception` and collapses everything into exit 1 + `FAILED (see logs)`. A `NotImplementedError` from `_build_provider`, a `ManagerNotConfiguredError` raised late by a factory, and a genuine vendor outage are indistinguishable on stdout. In a CronJob, stdout is often all an operator sees first. | C | Re-raise `ManagerNotConfiguredError`/`NotImplementedError` so they reach the exit-2 path; keep the blanket catch for genuine runtime failures and print `type(exc).__name__`. | S |
| T4 | `run_collector.py:551` | The carefully-worded `NotImplementedError` for `UCS_MANAGER` is **unreachable from the CLI**: `_run` calls `resolve()` first (`:943`), which raises `ManagerNotConfiguredError` for `UCS_MANAGER` with its own equally good message, exiting 2. The `_build_provider` branch is dead except from tests, and the same guidance now lives in two places free to drift. | M | Delete the branch and let `env.py`'s message be the single copy, or keep it and add a comment saying it is test-only. | S |
| T5 | `run_collector.py:426` + `:963` | `--limit N` is silently ignored without `--dry-run` (help text says "With --dry-run"), and no bound is checked: `--limit 0` prints `… stopped at --limit 0` and reports 0 servers; a negative value does the same. | C | `parser.error()` when `--limit` is given without `--dry-run`; reject `< 1`. | S |
| T6 | `run_collector.py:387` / `seed_inventory.py:50` / `loadtest.py:112` | No CLI value is range-checked anywhere in `tools/`. `loadtest.py --concurrency 0` constructs `asyncio.Semaphore(0)` and **hangs forever** with no output; `seed_inventory --count -5` runs to a confusing no-op. | C | One `_positive_int` argparse `type=` helper, shared. | S |
| T7 | `run_collector.py:970-999` vs `seed_inventory.py:83-107` | The full `IngestService` object graph (regex engine, both repos, classification service, health service, audit service, site + GPU catalogs) is constructed twice, ~25 near-identical lines each. They have already drifted once: `seed_inventory` builds **three** separate `MongoServerRepository` instances (`:88`, `:104`, `:122`) where `run_collector` builds one and reuses it. | M | One `build_ingest_service(mongo, settings)` in `app.application.services.ingest` (or a small `tools/_wiring.py`); both callers use it. | M |
| T8 | `verify_intersight.py:141` vs `verify_oneview.py:652`, `verify_ucs_central.py:70`, `run_collector.py:947` | **Exit-code inconsistency across the tool set.** Three tools return **2** for `ManagerNotConfiguredError`; `verify_intersight` returns **1**, mixing "you have not configured this" with "the appliance said no". A field operator following `docs/field-test-checklist.md` gets a different code for the same mistake depending on which probe they ran. | Co | `return 2` in `verify_intersight._run`. | S |
| T9 | `verify_intersight.py:225` | `settings.intersight_management_modes.split(",")` with **no `.strip()`**, where `run_collector._intersight_provider:279` splits *and* strips the same setting. A value written `"Intersight, IntersightStandalone"` makes the probe report "0 servers this collector would ingest" while the real collector ingests all of them — the probe would be read as a BAD verdict on working config. | C | Reuse one shared `_management_modes(settings)` helper. | S |
| T10 | `verify_ucs_central.py:80` | `await client.login()` sits **outside** the `try`, so a bad password or unreachable appliance escapes as a raw traceback — no verdict line, no controlled exit code — from a tool whose entire job is producing a legible verdict for a field engineer. `verify_intersight` and `verify_oneview` both handle this correctly. | C | Move `login()` inside the `try`, catch the client's error type, print `BAD — …`, return 1. | S |
| T11 | `verify_oneview.py:686` | Only `OneViewConnectionError` is caught; any other client error (auth, HTTP, JSON) escapes as a traceback, same class of problem as T10 but narrower. | C | Catch the client's base error class. | S |
| T12 | `verify_indexes.py:200` + `:219` | `_winning_stage_names` and `_index_names` are the *same* recursive plan-walk duplicated verbatim, differing only in the key they collect (`stage` vs `indexName`). | M | One `_walk_plan(plan, key) -> set[str]`. | S |
| T13 | `verify_indexes.py:251` | `explain["executionStats"]` is indexed unguarded. Absent (or renamed under a future driver/server) this is a `KeyError` traceback instead of a report line. | C | `.get("executionStats", {})` with defaults, or a clear message. | S |
| T14 | `loadtest.py:88` | An `httpx` transport exception inside `one_request` propagates out of `asyncio.gather` and kills the whole run mid-scenario. `errors` counts only non-2xx *responses*, so the failure mode the script most needs to survive is the one it does not. | C | `try/except httpx.HTTPError` inside `one_request`, count it as an error. | S |
| T15 | `loadtest.py:104` | `int(len(v) * pct)` is not nearest-rank: for 200 samples p99 returns index 198 rather than 197. Off by one sample in the tail, which is the part being measured. | C | `index = max(0, math.ceil(pct * len(v)) - 1)`. | S |
| T16 | `loadtest.py:94` | The latency of a failed (non-2xx) request is appended to `latencies` before the status check, so an error's (usually fast) latency skews the percentiles the run then reports. | C | Record only successful latencies, or report the two populations separately. | S |
| T17 | `seed_inventory.py:157-176` | `_seed_openshift` re-`upsert`s **every** server one at a time in a paging loop. At the stated 10k target that is 10,000 sequential round trips after the seed already ran; at 50k, 50,000. This is the slowest part of a `--count 50000` seed. | P | `bulk_write` in batches of 500, matching the page size already used. | M |
| T18 | `run_collector.py` (18 sites), `loadtest.py` (6), `verify_indexes.py` (10), `verify_ucs_central.py` (5) | **Convention 8 (docstrings) — measured, not assumed.** See table in §3. CLAUDE.md's claim that "`tools/verify_*.py`" were converted is true for 2 of 4 files and false for the other 2. | Co | Convert file by file when touched, and correct the CLAUDE.md sentence. | M |
| T19 | `verify_indexes.py:238` | `db: Any` on the one function doing real work, so `ty` checks nothing inside it. `tools/` **is** in the type gate (`ty check backend/app tools`), so this is a silently unchecked island. | M | Type it `AsyncDatabase[Any]` (or whatever `MongoClientHolder.db` is declared as). | S |
| T20 | `verify_oneview.py:673` | Hardware is fetched with a hardcoded `page_size=25` while profiles on the line above use `settings.oneview_page_size`. Since ADR-0022's open question 1 is precisely about paging behaviour, a hardcoded page size in the probe that is meant to settle it is a footgun. | Co | Use the setting for both. | S |

### Secrets in `tools/` output — checked, clean

Every credential-bearing line was read. No tool prints a password or a
private key. `verify_intersight.py:147` prints only whether the value looks
like a PEM (`'-----BEGIN' in api_key_pem`), never the key. `:146` prints the
API **Key ID**, which is an identifier, not a secret. `verify_ucs_central.py:79`
and `verify_oneview.py:653` print endpoint and username only.
`--debug-http`'s help text (`run_collector.py:409`) states that headers and
bodies are never logged and the session exchange is skipped rather than
redacted. `--debug-xml` (`:417`) is the one loud switch and its help says
so. Nothing to fix.

### Blocking calls / unnecessary `async` — checked, clean

No blocking vendor-SDK call is made from an async context in `tools/`; the
`ucsmsdk` wrapping happens inside `app.infrastructure.providers.ucs_manager.
client`, as CLAUDE.md requires. No `async def` in `tools/` is gratuitous:
each awaits something. `_debug_http_enabled`, `_or_unread`, `_format_*`,
`manager_for` and `collection_errors_of` are correctly synchronous.

---

## 3. Docstring conformance in `tools/` — measured

Produced by an AST pass counting, per function/method/class: a docstring
present; an `Args:` section when the function takes non-`self` parameters;
a `Returns:`/`Yields:` section when it returns something other than `None`.

| File | Total | Conforming | Partial (no `Args:`/`Returns:`) | Missing entirely |
|---|---|---|---|---|
| `verify_oneview.py` | 17 | **17** | 0 | 0 |
| `verify_intersight.py` | 14 | **14** | 0 | 0 |
| `run_collector.py` | 28 | 10 | 10 | 8 |
| `seed_inventory.py` | 4 | 1 | 0 | 3 |
| `verify_ucs_central.py` | 5 | 0 | 0 | **5** |
| `verify_indexes.py` | 10 | 0 | 0 | **10** |
| `loadtest.py` | 6 | 0 | 0 | **6** |
| **Total** | **84** | **42 (50%)** | **10 (12%)** | **32 (38%)** |

**CLAUDE.md is wrong on this point.** Convention 8 says the rule has been
"[a]pplied so far to … `tools/verify_*.py`". Two of the four `verify_*`
files (`verify_indexes.py`, `verify_ucs_central.py`) have **zero**
docstrings on **zero** of their 15 functions. The claim should name the two
files that were actually converted.

`run_collector.py`'s eight missing are `_parse_args` (:387), `_filtered`
(:513), `_run_one_manager` (:864), `_run` (:903), `main` (:1032),
`_NameFilteredProvider.__init__` (:475), `.health_check` (:490) and
`.list_servers` (:493) — i.e. the entry point and the whole run path, the
part of the file a new session reads first.

---

## 4. Load-bearing behaviour coverage matrix

The question is whether the behaviour is *tested*, not whether the file has
coverage. "Marked?" says whether the covering test carries a `unit` /
`integration` marker — see §5, `pytest -m unit` silently skips 38% of the
suite.

| # | Behaviour | Tested? | Where | Marked? |
|---|---|---|---|---|
| 1 | **Carry-forward `None` semantics** — a `None` carries the stored value, a real value overwrites | **YES**, thoroughly | `tests/unit/application/services/test_ingest_unread_fields.py` (5 tests on `_carry_forward` directly); `tests/integration/test_ingest_partial_reads.py:94` (`…does_not_erase_stored_hardware`) and `:161` (`test_an_empty_read_still_overwrites` — the negative case, the one that matters) | yes / yes |
| 2 | **`unread_fields` is recomputed, never merged** | **YES** | `test_ingest_unread_fields.py::test_the_accumulator_holds_only_what_was_passed_to_it` proves a fresh list per ingest; `test_ingest_partial_reads.py:263` proves the document records it | yes / yes |
| 3 | **Keyset cursor signing / verification** | **YES**, best-covered item in the suite | `tests/unit/domain/services/test_cursor.py` — 9 tests: round-trip (str + datetime), **tampered signature**, **tampered payload**, **wrong secret**, filter mismatch, sort change, page-size change, parametrised malformed input | **NO marker** |
| 4 | **ADR-0006 — dates compared as ISO-8601 strings, not `datetime`** | **YES, but only behaviourally and only as an integration test** | `tests/integration/test_audit_event_repository.py:76` `test_pagination_covers_every_event_exactly_once` — 25 events at page_size 7. A `datetime` operand against string-stored `created_at` is a cross-BSON-type comparison returning nothing, so page 2 would come back empty and `assert len(seen) == 25` fails. The `"Z"` vs `"+00:00"` variant (the comment at `audit_event_repository.py:131`) also fails it, via the duplicate assertion. **There is no unit-level test naming the invariant**, and with the dev stack down the whole thing skips. | integration |
| 5 | **`policy_key` family shadowing (ADR-0005)** | **YES**, well | `tests/unit/domain/services/test_health_evaluate.py` — `test_site_override_shadows_global_default_for_that_site_only` (:171), `test_shadowed_family_member_is_recorded` (:217), `test_different_policy_keys_fire_independently` (:294) | **NO marker** |
| 6 | **Site parsing from name, incl. ambiguous-yields-`None`** | **YES** | `tests/unit/domain/test_site_parsing.py:100` `test_ambiguous_name_with_two_sites_returns_none_rather_than_guessing`, `:104` `test_the_same_site_repeated_is_not_ambiguous` | yes |
| 7a | **Name-pattern filter** (`_NameFilteredProvider`) | **YES** | `tests/unit/tools/test_run_collector.py::TestNameFilter` (:657) — keeps only matching, `re.search`-not-`re.match`, empty pattern returns the provider unwrapped, `health_check` still reaches the inner provider, dry run shows only what a real run writes | yes |
| 7b | **The `REDFISH_STANDALONE` exemption** (`_UNFILTERED_TYPES`) | **NO — NOT TESTED** | `_UNFILTERED_TYPES` appears **nowhere** in `tests/`. Nothing asserts that `_run(manager_type=REDFISH_STANDALONE)` passes `name_pattern=""`. Flipping that line would make a `^ocp` deployment silently ingest **zero** standalone BMCs — no error, exit 0, "0 kept, 900 skipped" in a log nobody reads. | — |
| 7c | **`_ENDPOINTLESS_TYPES`** (the inventory-file `ManagerConnection`) | **NO — NOT TESTED** | Same absence. Nothing asserts `_run` for `REDFISH_STANDALONE` skips `resolve()` and builds the connection from `settings.redfish_inventory_file`; regressing it exits 2 demanding a variable that deliberately does not exist. | — |
| 8 | **GPU catalog: equality on a normalized key, never substring** | **YES**, exemplary | `tests/unit/domain/test_gpu_catalog.py:233` (`A10` vs `A100`, one character and 3× the VRAM), `:252` (the four two-capacity models `A100`/`V100`/`H100`/`P100` match **nothing** and keep `memory_bytes: None`), `:364` (rejects a longer string containing a valid model) | yes |
| 9 | **PSU: `Absent` is dropped, never counted as failed** | **YES** | `tests/unit/infrastructure/providers/test_redfish_psus.py:77` `TestAbsentBays::test_an_absent_supply_is_dropped_entirely` (two absent bays in a four-bay chassis); `tests/redfish_fixture.py:264` builds the empty-bay payload; `test_redfish_collector.py:392` covers the analogous stale-`CapacityMiB`-but-`Absent` drive | **NO marker** |
| 9b | **PSU health vocabulary is `UP`/`DOWN`/`DISABLED`/`UNKNOWN`, and Redfish `Warning` -> `UNKNOWN` not `DOWN`** | **YES** | `test_redfish_psus.py` (file docstring names it as the point of the file) | **NO marker** |
| 10 | **Collector exit codes 0 / 1 / 2 / 3** | **YES**, all four | `tests/unit/tools/test_run_collector.py::TestRunExitCodes` (:794) — see §1's table | yes |
| 10b | **`--dry-run` exit codes (0 on success, 1 on failure)** | **NO — NOT TESTED** | `TestDryRun` asserts printed output and counts only; no test drives `_run(dry_run=True)` to an exit code. The dry-run 1 path (`:966`) is uncovered. | — |
| 11 | **RFC 9457 problem envelope** | **PARTIAL** | `tests/api/test_servers.py:205` and `tests/api/test_health.py:39` assert `content-type` starts with `application/problem+json` and check `code`, `status`, `request_id`, `instance`. **No test anywhere asserts `type`, `title` or `detail`** — and `type`/`title` are the two members RFC 9457 actually names as core. `_problem_response` (`backend/app/exception_handlers.py:43`) emits all three. Dropping `title` would pass the suite. | **NO marker** (all of `tests/api/`) |

### Summary — behaviours with NO test

1. **`_UNFILTERED_TYPES` / the `REDFISH_STANDALONE` name-pattern exemption** (7b) — high
2. **`_ENDPOINTLESS_TYPES`** (7c) — high
3. **`--dry-run` exit codes** (10b) — medium
4. **RFC 9457 `type` / `title` / `detail` fields** (11) — medium

Everything else on the brief's list is genuinely covered, and several
(cursor tampering, GPU equality-not-substring, `_carry_forward`) are
covered better than the average codebase manages.

---

## 5. `tests/` findings

| # | file:line | Finding | Cat | Fix | Eff |
|---|---|---|---|---|---|
| S1 | `tests/api/*` (7 files, 52 tests) | **`tests/api/` has no `conftest.py` and no skip-when-unreachable guard.** The memoized-`_UNREACHABLE` mechanism that fixed the "suite looks hung" problem was applied to `tests/integration/` **only**. Measured: with Mongo pointed at a dead port, `tests/integration/test_ingest.py` → *5 skipped*, `tests/api/test_health.py` → *3 **errors***, 21.23s for three tests. Extrapolated over 52 api tests that is ~4–5 minutes of red — the exact symptom CLAUDE.md records as fixed. Even `test_liveness_always_ok`, which needs no database, errors: the app's lifespan is what fails. | C | `tests/api/conftest.py` reusing the same `_UNREACHABLE` memo (lift it into a shared helper importable by both conftests). | M |
| S2 | 25 of 59 test files | **`pytest -m unit` silently deselects 395 of 1,037 tests (38%).** Measured: `pytest --collect-only -q` → 1,037; `pytest -m unit --collect-only -q` → `642/1037 (395 deselected)`. Unmarked and therefore invisible to `-m unit`: **all 7 `tests/api/` files** (the whole RFC 9457 and endpoint contract), **all 9 `tests/unit/domain/services/`** files (cursor signing, `policy_key` shadowing/ADR-0005, health evaluation, classification, regex engine, search tokens), `test_generator.py` (the seeder drift guard), `test_redfish_psus.py` (the PSU `Absent` rule), `test_openmanage_nics.py`, both `value_objects` files, and both `application/services` files. A green `-m unit` run therefore proves nothing about ADR-0005, ADR-0006 or the API contract. | C | Either add `pytestmark` to the 25 files, or delete the markers entirely and let the directory be the selector — do not leave the middle state. | M |
| S3 | `tests/unit/infrastructure/providers/test_openmanage_provider.py:22` | `pytestmark = pytest.mark.asyncio` — a file *inside* `tests/unit/` carrying a marker that is not `unit`, so it is deselected by `-m unit` too. The marker is also **redundant**: `asyncio_mode = "auto"` (pyproject.toml:108) already collects every `async def test_`, and `tests/conftest.py:12` says so in a comment. | Co | `pytest.mark.unit`. | S |
| S4 | `tests/api/test_servers.py:68`, `test_sites.py:81`, `test_server_facets.py:82`, `test_maintenance_and_events.py:100`, `test_classification_rules.py:95`, `test_health_policies.py:108` | The **same ~20-line `app_context` fixture is defined six times** — identical `create_app()` + `ASGITransport` + `lifespan_context` + collection-wipe + Redis `flushdb` + repo construction. Which collections get wiped already differs between copies (`test_servers` wipes `servers`/`sites`/`managers`; others differ), so they have drifted. This is also exactly where S1's fix belongs. | M | One fixture in `tests/api/conftest.py`, parametrised on the collections to wipe. | M |
| S5 | `tests/` (52 sites) | **52 coded `# type: ignore[...]` suppressions are inert.** CLAUDE.md convention 7 names this trap explicitly: ty honours a *bare* `# type: ignore` but not a coded one. Concentrated in `test_redfish_targets.py` (9), `test_run_collector.py` (~8), `test_intersight_client.py`. Harmless today because `tests/` is outside the gate (`ty check backend/app tools`) — but they are mypy-era fossils that read as active suppressions, and adding `tests` to the gate would light up 52 real diagnostics at once. `tools/` is clean (0). | M | Delete them, or convert to `# ty: ignore[rule]` if a real diagnostic appears when `tests/` joins the gate. | S |
| S6 | `tests/integration/test_audit_event_repository.py:76` | The only ADR-0006 regression test is integration-only and *incidental* — it asserts uniqueness and total count, never ordering, never the tie-break `_id` branch, and its docstring/name say nothing about string-vs-`datetime` comparison. A future reader optimising it (e.g. reducing to one page) would silently delete the repo's only protection against a bug ADR-0006 records as having produced real silent-wrong results. | C | Add a unit test over `_decode_cursor`/`_encode_cursor` asserting the operand is a `str`, and rename the integration test to name the invariant. | S |
| S7 | `tests/unit/tools/test_run_collector.py:794` `TestRunExitCodes` | All four exit-code tests drive `_run` with `ManagerType.UCS_CENTRAL` and monkeypatch `_run_one_manager` away. Correct for what they assert, but it means the *dispatch* around exit codes — endpointless connection building, the `_UNFILTERED_TYPES` pattern choice, the `UCS_CENTRAL` second-login pre-flight ordering, the dry-run branch — is exercised for exactly one manager type. Findings 7b, 7c and 10b in §4 all live in this blind spot. | C | Parametrise over `ManagerType`, asserting the `name_pattern` and `ManagerConnection` each type produces. | M |
| S8 | `tests/unit/infrastructure/providers/test_generator.py:65` | The guard is **sound and cannot drift** — `expected = frozenset(PROVIDER_FACTORIES) - _UNSEEDED_COLLECTORS`, importing the real dict at `:9`. Verified by running it: passes. But it is **one-directional**: it catches a collector in `PROVIDER_FACTORIES` with no seeded shape, not a `COLLECTOR_TYPES` entry whose collector was removed. `test_frontend_manager_types.py` gets this right, with a test in each direction (`:60` and `:74`). | M | Add the mirror assertion, matching the frontend guard. | S |
| S9 | `tests/unit/infrastructure/providers/test_generator.py:62` | **`_UNSEEDED_COLLECTORS` is now `frozenset()` — empty.** `COLLECTOR_TYPES` (`generator.py:68`) lists all five collectors including `OPENMANAGE`, and `provider_type_for` (`:203`) discriminates Dell by `server.vendor` rather than `external_id` prefix. **The work CLAUDE.md lists as outstanding item 2 ("Give the Dell collector a seeded shape") is already done.** CLAUDE.md is stale. | Co | Correct the "Where to continue right now" section. | S |
| S10 | `tests/` (579 of 1,059 functions) | 55% of test functions have no docstring. Convention 8 says "every function, method and class". Reported as measured; the names are long and declarative (`test_ambiguous_name_with_two_sites_returns_none_rather_than_guessing`) and arguably carry the intent already. **Flagged for the user to rule on, not asserted as a defect** — see §7. | Co | User decision. | L if enforced |
| S11 | `tests/integration/conftest.py:35` | The `_UNREACHABLE` memo is a module-level mutable dict. It is **sound**: write-once per key, never read across processes (xdist workers are separate interpreters), and the docstring correctly documents the deliberate trade — a service that comes up mid-run stays skipped. The one sharp edge is that `mongo_holder` catches only `PyMongoError`; a non-PyMongo failure (an `OSError` from a bad URI scheme) would propagate rather than memoize, reintroducing S1's symptom for that case. Verified working: 5 clean skips against a dead port. | M | Widen to `(PyMongoError, OSError)`. | S |

### Test quality — what the scan looked for and did not find

Explicitly checked, because "over-mocking that tests the mock" was on the brief:

- **`unittest.mock` usage across the whole suite: zero.** No `MagicMock`, no
  `AsyncMock`, no `mock.patch`. Every double is a hand-written fake class
  (`FakeCredentialResolver`, `FakeIngestService`, `FakeMongo`,
  `_OneShotProvider`, `TestNameFilter._Fake`, `tests/redfish_fixture.py`).
  This is the single best structural property of the suite: a hand-written
  fake that drifts from the real Protocol fails to satisfy it, where a
  `MagicMock` accepts anything and keeps passing.
- **`monkeypatch`: 29 uses**, and they are proportionate. 20 set environment
  variables or replace `get_settings`; the rest replace a module-level
  symbol in the module under test (`run_collector._build_provider`,
  `run_collector.MongoClientHolder`) or stub `asyncio.sleep` to keep a
  retry test fast. No patching of a collaborator's internals.
- **Assertions that cannot fail:** none found (`assert True`, `assert 1 == 1`,
  bare `is not None` on a just-constructed object).
- **Shared mutable module-level fixture state:** only
  `tests/integration/conftest.py:35`, analysed at S11.
- **Order dependence:** none observed; every stateful fixture wipes its
  collections both before *and* after yielding.
- **Annotation completeness (the ANN ratchet, convention 7):** measured
  across all 1,059 test functions — **zero** missing parameter or return
  annotations. The ratchet is doing its job.

---

## 6. What is already good

Not a courtesy section — these are things a hardening pass should be
careful **not** to break.

1. **The `ManagerNotConfiguredError` messages.** Every path names the exact
   `INVENTORY_*` variables to set, and the two types with no endpoint by
   design (`UCS_MANAGER`, `REDFISH_STANDALONE`) get bespoke text explaining
   *why* there is no variable and what to configure instead. This is the
   difference between a five-minute fix and an afternoon. Preserve verbatim.

2. **Exit code 3.** Distinguishing "partial run" from "success" is a design
   decision most inventory collectors get wrong, and the comment at `:1020`
   states exactly why: reported as 0, a bad credential on one domain is
   indistinguishable from a healthy run against a smaller estate.

3. **Two guards that derive rather than restate.** `test_frontend_manager_types.py`
   and `test_generator.py` both import `PROVIDER_FACTORIES` instead of
   listing collectors, and both carry a comment saying the previous
   hand-written version drifted along with the thing it guarded and stayed
   green. That lesson is correctly learned and correctly written down.
   `test_frontend_manager_types.py` guards in **both** directions.

4. **Zero mocking.** See §5. Hand-written fakes throughout.

5. **The dry-run/`IngestService` split.** `--dry-run` bypassing the pipeline
   entirely — rather than passing a no-op repository — is the right call
   and the reason the name filter had to be a provider wrapper. Both halves
   of that reasoning are written down *and* tested
   (`test_dry_run_shows_only_what_a_real_run_would_write`).

6. **`verify_intersight.py` and `verify_oneview.py`.** 31 functions, 100%
   docstring conformance, error handling that produces a verdict rather
   than a traceback, and — the part that matters — each open ADR question
   is answered by a named section that prints `GOOD` / `PARTIAL` / `BAD` /
   `INCONCLUSIVE`, with `INCONCLUSIVE` distinguished from failure. A field
   engineer can run these and bring back an answer. These are the model the
   other two `verify_*` tools should be brought up to.

7. **The `_carry_forward` / `unread_fields` test file.** Five short tests
   that pin a genuinely subtle invariant, including the two cases people
   get wrong: a read-and-genuinely-empty `[]` is *not* recorded as unread,
   and the accumulator is per-run so nothing leaks forward.

8. **`test_gpu_catalog.py`.** The `A10`-vs-`A100` test and the "two-capacity
   models match nothing" test are exactly the tests ADR-0021's reasoning
   demands, and they are written to fail loudly if someone "improves"
   matching into a substring check.

9. **The integration-fixture memoization.** Mechanism verified empirically:
   5 clean skips in 0.2s against a dead Mongo, where the naive version paid
   5s per test. The docstring explains the trade honestly.

10. **The lint gate is green.** `ruff check .`, `ruff format --check .`
    (213 files) and `ty check backend/app tools` all pass as of this audit.

---

## 7. Questions for the user

1. **Exit code 2 is overloaded.** `argparse` exits 2 on a usage error and
   `_run` exits 2 on "not configured". A CronJob's alerting cannot tell a
   typo'd `--manager-type` from an unset Secret. Should the not-configured
   code move (to 4, say), or is the exit-code table frozen as a contract
   something already depends on? I changed nothing pending your answer.

2. **Is `pytest -m unit` meant to be a usable gate?** Right now it selects
   62% of the suite and skips ADR-0005, ADR-0006 and the entire API
   contract. Two clean options: mark the 25 unmarked files, or delete both
   markers and select by directory. The middle state is the dangerous one.
   Which?

3. **Docstrings on test functions** — 579 of 1,059 have none. Convention 8
   reads as universal, but test names here are long and declarative and
   arguably already carry the intent. Does convention 8 apply to `tests/`,
   or should it say so explicitly either way?

4. **CLAUDE.md has two stale claims this audit found.** (a) "Applied so far
   to … `tools/verify_*.py`" — true for 2 of 4 files; `verify_indexes.py`
   and `verify_ucs_central.py` have zero docstrings. (b) "Where to continue
   right now" item 2, the Dell seeder gap — already done:
   `_UNSEEDED_COLLECTORS` is empty, `COLLECTOR_TYPES` has all five, and
   `provider_type_for` discriminates Dell by vendor. Want these corrected as
   part of this pass?

5. **Priority between S1 and S2.** Both are "the suite lies to you"
   problems. S1 (api tests error rather than skip with the stack down) is
   the one that costs minutes on every developer run; S2 (`-m unit` skips
   38%) is the one that could let a real regression through. My
   recommendation is S1 first because it is smaller and shares its fix with
   S4, then S2 — but you may weight the false-confidence risk higher.

6. **`verify_ucs_central.py` and `verify_indexes.py`** are the two tools
   that have not had the treatment the other two got (docstrings, verdict
   sections, controlled error paths). Bring them up to the
   `verify_intersight`/`verify_oneview` standard in this pass, or leave
   them until the field trip that would actually exercise them?

---

## 8. Test-suite execution note

**I did not run the full suite, and I started no container stack.** A dev
stack (`server-inventory-dev-mongo`, `server-inventory-dev-redis`, via
`scripts/dev-up.sh`) was **already running** when this audit began, started
by someone else in this session; per the "tear down what you start" rule I
left it exactly as found and tore down nothing.

What I did run, all read-only or against a deliberately dead endpoint so no
shared database was touched:

- `pytest --collect-only -q` and `pytest -m unit --collect-only -q` —
  collection only, for the 1,037 / 642 figures in S2.
- `pytest tests/unit/infrastructure/providers/test_generator.py::test_the_seeder_shapes_every_implemented_collector` —
  1 passed, to verify the drift guard (S8/S9).
- `INVENTORY_MONGO_URI=mongodb://127.0.0.1:27099 pytest tests/api/test_health.py tests/integration/test_ingest.py` —
  pointed at a **dead port**, to measure S1's skip-vs-error asymmetry
  without touching `server_inventory_test`. Result: `5 skipped, 3 errors in
  21.23s`.
- `ruff check .`, `ruff format --check .`, `ty check backend/app tools` — all clean.
- Two throwaway AST scripts in the session scratchpad (docstring and
  annotation censuses); nothing written under the repo.
