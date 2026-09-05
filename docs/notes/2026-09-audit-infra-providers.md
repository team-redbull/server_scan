# Infrastructure & providers audit — 2026-09

Read-only audit of `backend/app/infrastructure/` for the production-hardening
pass. **No source code was changed.** Branch `dev-refactor`.

Scope: `providers/` (all seven), `mongodb/`, `redis/`, `credentials/`,
`logging/`, `singleflight.py`. `app/observability/metrics.py` is outside this
directory but is referenced where `redis/cache.py` uses it.

ADRs read before judging anything under `providers/`: 0003, 0005, 0006, 0007,
0009, 0014, 0016, 0017, 0020, 0021, 0022, plus `docs/cisco-collectors.md`,
`docs/dell-collectors.md`, `docs/hpe-collectors.md`.

---

## 1. The current contract

`app/domain/ports/provider.py:166-172` — the whole Protocol:

```python
class ServerInventoryProvider(Protocol):
    provider_type: str
    async def health_check(self) -> None: ...
    def list_servers(self) -> AsyncIterator[ProviderServer]: ...
```

Three members. Everything else every collector actually does — partial-failure
accounting, run budgets, per-run counters, client construction, session
teardown, name filtering, run summaries — is outside the contract and
re-invented per vendor. That is the gap this pass exists to close, and the
matrix below is the input to designing the replacement.

---

## 2. The seven-provider capability matrix

Legend: **y** = present, **n** = absent, **n/a** = does not apply.

### 2.1 Construction, authentication, session

| | `fake` | `ucs_manager` | `ucs_central` | `intersight` | `openmanage` | `oneview` | `redfish` |
|---|---|---|---|---|---|---|---|
| Client construction | none | `_new_client()` `provider.py:69` | `_new_client()` + `client_factory` seam `provider.py:240,235` | `_new_client()` + `client_factory` seam `provider.py:276` | `_new_client()` `provider.py:146` | `_new_client()` + `client_factory` seam `provider.py:141,139` | `_new_client(target)` + `client_factory` seam `provider.py:175,173` |
| Transport | n/a | `ucsmsdk.UcsHandle` (sync, `asyncio.to_thread`) | `ucscsdk.UcscHandle` (sync, `asyncio.to_thread`) + wrapper deadline | `httpx.AsyncClient` | `httpx.AsyncClient` | `httpx.AsyncClient` | `httpx.AsyncClient` |
| Auth model | none | login per domain | Central login **plus** one UCS Manager login replayed per discovered domain | **no login** — every request signed `hs2019` (`signing.py`) | **two logins**: OME appliance + one shared iDRAC account | one login, `Auth:` header (bare `sessionID`, not Bearer) | one session per BMC, `X-Auth-Token`; credential resolved per host from a chain |
| Session count per run | 0 | 1 per `list_servers()` call | 1 to Central (in `_plan`) + 1 per domain + 1 more to Central per `health_check` | 0 (stateless) | 1 to OME (`_discover`), then N BMC sessions via the Redfish pass | 1 | 1 per host, N hosts |
| Explicit logout / release | n/a | **y** `client.py:112` (`finally`) | **y** `client.py:137` (`finally`, both `_plan` and `health_check`) | **y** `aclose()` in `finally` `provider.py:617` | **y** `__aexit__` → `logout()` + `aclose()` | **y** `__aexit__` → DELETE `/rest/login-sessions` + `aclose()` | **y** `__aexit__`, DELETE **shielded from cancellation** `client.py:249` |
| Endpoint validated | n/a | `_validate_endpoint` `client.py:31` | `_validate_endpoint` `client.py:30` | `validate_endpoint` `client.py:72` | bare `.strip()` only `client.py:83` | bare `.strip()` only `client.py:118` | `_normalize_host` in `targets.py:99` (rejects embedded creds) |
| TLS posture | n/a | SDK default | SDK default | **`verify=False`, unconditional, no setting** `client.py:166` | `verify=verify_tls`, default `False`, **no setting wires it** | `verify=verify_tls` ← `INVENTORY_ONEVIEW_VERIFY_TLS` | per-host `ssl.SSLContext`, verification **on** unless the host opts out with a written reason |

### 2.2 Run behaviour

| | `fake` | `ucs_manager` | `ucs_central` | `intersight` | `openmanage` | `oneview` | `redfish` |
|---|---|---|---|---|---|---|---|
| `collection_errors` | **n** | **n** | **y** `provider.py:281` | **y** `provider.py:262` | **y** `provider.py:136` | **n** | **y** `provider.py:194` |
| …reset at start of run | n/a | n/a | **y** `:500` | **n** — accumulates across iterations | **y** `:189` | n/a | **y** `:239` |
| List/detail separated | no (one generator) | no — 14 fleet-wide `query_classid` calls, joined in memory | **yes** — Central `_plan()` lists, `UcsManagerProvider` fetches per domain | **yes** — `_build_joins()` then streamed `compute/PhysicalSummaries` | **yes** — OME `_discover()` lists, Redfish fetches per host | **yes** — `_matched()` joins bulk lists, `_power_supplies()` per server | **yes** — inventory file lists, `_collect_systems()` fetches per host |
| Concurrency | n/a | none (serial queries) | `Semaphore(ucs_central_domain_concurrency)` `:502` | none needed (fleet-wide queries) | inherits the Redfish pass's | `Semaphore(psu_concurrency)` `:339` for the PSU fan-out only | `Semaphore(fleet_concurrency)` `:248` |
| Streams as results land | n/a | yields per server | **y** `asyncio.as_completed` `:517` | **y** (joins first, then streams servers) | **y** (passes the Redfish generator through) | **n** — whole appliance materialised, then yielded | **y** `asyncio.as_completed` `:253` |
| Run budget | n/a | **n** | **n** (deferred — ADR-0014 names it) | **y** `run_budget_seconds`, checked between join tables and per server | inherits the Redfish pass's | **n** | **y** `asyncio.timeout(run_budget)` + per-host `asyncio.timeout(host_budget)` |
| Per-run counters kept | none | none | `collection_errors`; per-domain `collected` | `collected`, `len(_collection_errors)` | only what the Redfish pass counts | `unassigned`, `filtered`, `unreadable` Counter, PSU `failures` | `collected`, `hosts_failed`, `auth_failures`, `credentials_disabled` |
| Log at run start | none | none | `ucs_central.domain_plan` `:351` | none | `ome.enumerated` `:230`, `ome.collecting_over_redfish` `:195` | none | none |
| Log at run end | none | none | per-domain `ucs_central.domain_summary` `:555` (no whole-run line) | `intersight.run_summary` `:610` | none of its own | `oneview.collected` `:195` (emitted **before** the yields) | `redfish.run_summary` `:297` |
| Credential-damage guard | n/a | n | n | n | inherits Redfish's | n | **y** `_AuthGuard` — per-credential circuit + run-wide budget `:57-114` |

### 2.3 How each enforces "`None` means could not read, never zero"

| | Mechanism |
|---|---|
| `fake` | n/a — generates complete servers. |
| `ucs_manager` | Per-field in `mapping.py`; an MO class that returns nothing yields an empty bucket, which for PSUs is a documented capability gap (a blade's PSUs belong to its chassis), not a `None`. |
| `ucs_central` | Inherits `ucs_manager`'s mapping wholesale; adds nothing. |
| `intersight` | **The strongest of the seven.** `_Joins` (`provider.py:162`) holds every sub-resource table as `dict | None`; `_collect_table` returns `None` on failure *and* on budget skip; `for_server()` (`:184`) preserves the distinction per server (`None` table → `None`, read table → `[]`). |
| `openmanage` | Inherits Redfish's `_optional`; the merge (`_merged`, `:302`) only fills `model`/`serial` where the BMC reported nothing, so measured always wins. |
| `oneview` | `collectionState != "Collected"` → `None` (`mapping.py:455,570`); `InsufficientFirmware` and `CollectedStale` both map to unread. PSU fetch failure → `None`, aggregated count only. |
| `redfish` | `_optional` / `_optional_link` / `_drives` / `_psus` all return `None`, never `[]`, on 403/protocol/unreachable (`provider.py:548,654,581,685`). |

### 2.4 What is genuinely vendor-specific — do **not** force into a common shape

- **UCS Central's two-tier discovery.** It is a directory, not an inventory
  source; the per-domain `UcsManagerProvider` is the real collector. Any base
  class must let one provider *be* a fan-out over other providers.
- **Intersight's signing and its inverse-reference join model.** No login to
  put in a base class; its cost model (flat in fleet size, memory-bound) is the
  opposite of everyone else's.
- **OpenManage's two credential domains** (OME + iDRAC) and its
  identity-here/hardware-there split (ADR-0020).
- **OneView's single-standard decision** (ADR-0022) — no Redfish pass, no BMC
  credentials, `mpModel` read but never branched on. Closed to re-litigation.
- **Redfish's `_AuthGuard`.** Only a per-host fan-out can lock accounts; a
  base class should offer it, never impose it.
- **Redfish's TLS-verification-on-by-default with a reason-gated per-host
  opt-out.** This is the *correct* posture and the one the others should move
  toward, not something to average away.
- **The `ucsmsdk`/`ucscsdk` `asyncio.to_thread` wrappers.** Sync SDKs; nothing
  httpx-shaped applies.
- **Per-vendor "what is unread" semantics.** `collectionState` (HPE),
  `_Joins is None` (Cisco), `_optional` (Redfish) are three different truths
  about three different APIs.

---

## 3. What `collection_errors_of` papers over

`tools/run_collector.py:517-534`:

```python
def collection_errors_of(provider: object) -> tuple[str, ...]:
    return tuple(getattr(provider, "collection_errors", ()) or ())
```

Its docstring justifies the duck-typing as "only a collector that fans out over
several endpoints can partially fail, so requiring the attribute of every
provider … would be ceremony for a single vendor's shape."

**That justification no longer holds, and the gap it papers over has three
distinct parts:**

1. **It is no longer one vendor's shape — it is four of seven** (`ucs_central`,
   `intersight`, `openmanage`, `redfish`). The `object` parameter type is the
   tell: the function cannot be typed against the Protocol because the Protocol
   does not know the concept exists.

2. **It cannot distinguish "complete run" from "provider does not track this".**
   Both return `()`. `run_collector` maps `()` to exit 0 = success. So
   **`oneview` — which detects truncation (`oneview.collection_truncated` at
   ERROR, `client.py:356`), counts unreadable PSUs, counts unreadable
   subresources — reports every one of those as a clean run and exits 0.** A
   OneView appliance that returned a third of the estate is indistinguishable
   from a healthy run against a smaller fleet, which is exactly the failure
   ADR-0022 says to watch for.

3. **It has already been copy-pasted.** `openmanage/provider.py:205` does its
   own `getattr(redfish, "collection_errors", ())` rather than calling the
   helper — a second, independent instance of the same hole, inside a provider
   this time. A real contract member removes both.

A replacement base class should make `collection_errors` a required member with
a default empty implementation, so "reports none" and "cannot report" stop
being the same value.

---

## 4. Duplication inventory

Measured across the seven providers. Line counts are the duplicated bodies, not
the whole files.

| Cluster | Copies | ≈ lines | Where |
|---|---|---|---|
| `_new_client()` — build a client from stored config | 6 | ~90 | `ucs_manager:69`, `ucs_central:240`, `intersight:276`, `openmanage:146`, `oneview:141`, `redfish:175` |
| `client_factory` test-seam plumbing (`factory or self._new_client`) | 4 | ~20 | `ucs_central:235`, `intersight:258`, `oneview:139`, `redfish:173` |
| `health_check()` = open a client, close it, do nothing else | 5 | ~40 | `ucs_manager:84`, `ucs_central:299`, `intersight:304`, `openmanage:162`, `oneview:157` |
| `if not manager.endpoint: raise ValueError(...)` | 4 | ~10 | `ucs_manager:62`, `ucs_central:225`, `openmanage:119`, `oneview:127` |
| `_collection_errors: list[str]` + `collection_errors` property + docstring | 4 | ~55 | `ucs_central:234,281`, `intersight:259,262`, `openmanage:133,136`, `redfish:172,194` |
| Endpoint validation (bare-host, no scheme, no port) | 3 | ~90 | `ucs_manager/client.py:31`, `ucs_central/client.py:30`, `intersight/client.py:72` — near-identical, differing only in product name and error text |
| Logout: try / swallow / `logger.warning("<v>.logout_failed", …)` | 5 | ~40 | `ucs_manager/client.py:121`, `ucs_central/client.py:147`, `openmanage/client.py:161`, `oneview/client.py:277`, `redfish/client.py:247` |
| Hand-rolled pagination loop | 4 | ~110 | `intersight/client.py:202` (`$top`/`$skip`), `openmanage/client.py:170` (`@odata.nextLink`), `oneview/client.py:287` (`nextPageUri` + truncation check), `redfish/client.py:376` (`Members@odata.nextLink`) |
| `httpx.AsyncClient` construction | 4 | ~35 | Four different postures — see §5, finding I-5 |
| Compile a name regex and `.search()` it | 4 | ~25 | `run_collector:_NameFilteredProvider`, `openmanage:_matches:262`, `oneview:_matched:257`, `ucs_central:domains_to_collect:149` |
| Streaming fan-out: `Semaphore` + `create_task` + `as_completed` | 2 | ~45 | `ucs_central:502-521`, `redfish:248-280` — same pattern, only Redfish cleans up its tasks (see C-2) |
| "Run summary" log line | 4 shapes | — | `redfish.run_summary`, `intersight.run_summary`, `oneview.collected`, `ucs_central.domain_summary` — **no shared key names**, so no dashboard can aggregate across collectors |

**Total mechanically duplicated: roughly 520 lines** across the seven
providers, before counting the four incompatible summary-log shapes.

The base class should absorb: client construction + the factory seam,
`health_check` as "open and close", the endpoint guard, error accumulation
(with reset-per-run built in), the name filter, the bounded streaming fan-out
with guaranteed task cleanup, and one run-summary shape with stable key names.
It should **not** absorb: pagination (four genuinely different protocols),
authentication, or the "unread" semantics.

---

## 5. Findings, ranked

Severity: **C** correctness · **M** maintainability · **P** performance ·
**I** consistency. Effort S/M/L.

### Correctness

| # | File:line | Finding | Fix | Eff | ADR |
|---|---|---|---|---|---|
| C-1 | `oneview/provider.py:69` (class body) | **No `collection_errors` at all.** The client detects truncation and logs `oneview.collection_truncated` at ERROR (`client.py:356`), the provider counts unreadable subresources (`:280`) and unreadable PSUs (`:373`) — none of it reaches `run_collector`, so a truncated run **exits 0**. | Add the property; record truncation, PSU failures and unreadable subresources into it. Needs the client to report truncation back rather than only logging. | M | 0022 (see §7 Q1) |
| C-2 | `ucs_central/provider.py:514-521` | `list_servers` creates one task per domain but has **no `try/finally` cancelling them**. If the consumer breaks early or closes the generator, in-flight domain tasks are orphaned — their UCS Manager sessions are never logged out, against a per-user session cap. `redfish/provider.py:275-280` does exactly this cleanup; only half the pattern was ported. | Wrap the `as_completed` loop in `try/finally`, `task.cancel()` then `await asyncio.gather(*tasks, return_exceptions=True)`, copying Redfish. | S | 0014 |
| C-3 | `openmanage/provider.py:202-205` | `self._collection_errors.extend(...)` runs **after** the `async for`. If the consumer closes the generator early (`GeneratorExit`) or the Redfish pass raises, every per-host error is lost and the run reports clean. Also iterates without `contextlib.aclosing`, so the inner generator's `finally` (task cancel + session teardown) is deferred to GC. | `async with contextlib.aclosing(redfish.list_servers()) as servers:` and move the `extend` into a `finally`. | S | 0020 |
| C-4 | `intersight/provider.py:552-566` | `list_servers` never clears `_collection_errors`. A second iteration of the same provider reports the first run's failures again. `ucs_central:500` and `redfish:239` both reset; this one was missed. | `self._collection_errors.clear()` at the top of `list_servers`. | S | 0017 |
| C-5 | `ucs_manager/client.py:126-149` + `ucs_central/provider.py:397-449` | **No deadline anywhere on the per-domain path.** `UcsManagerClient` passes `timeout=` to `UcsHandle` (per-socket only, its own docstring says so) and, unlike `UcsCentralClient._with_timeout` (`client.py:92`), imposes no `asyncio.wait_for`. `_collect_domain` has no per-domain budget either. One wedged domain holds a semaphore slot indefinitely and the whole run dies at `activeDeadlineSeconds` with no summary. | Two parts: a `_with_timeout`-equivalent in `UcsManagerClient` (mirror `ucs_central/client.py`), and a per-domain `asyncio.timeout` in `_collect_domain` + a run budget. **ADR-0014's closing section already names the run budget as deliberately deferred** — the per-domain deadline is the newer half of this. | M | 0014 (deferred there) |
| C-6 | `oneview/provider.py:356-357` | `except Exception: return uri, None` — a bare swallow with **no logging of the reason**. Only an aggregate count survives (`:373`), so "all 400 PSU calls failed because the session expired" and "two hosts were slow" look the same. | `logger.warning("oneview.power_supplies_failed", uri=..., error=str(exc))` before returning `None`; narrow to `OneViewConnectionError` and let anything else propagate. | S | 0022 |

### Consistency

| # | File:line | Finding | Fix | Eff |
|---|---|---|---|---|
| I-1 | `openmanage/client.py:64,92-96` + `config/settings.py` | `OmeClient.verify_tls` defaults `False` and **no setting wires it** — `tools/run_collector.py:_openmanage_provider` passes `bmc_verify_tls` and `bmc_ca_bundle` but never `verify_tls`. So the OME *appliance* connection is unconditionally unverified while its BMCs have `INVENTORY_OME_BMC_VERIFY_TLS` and OneView has `INVENTORY_ONEVIEW_VERIFY_TLS`. A dead parameter that reads as configurable. | Add `ome_verify_tls: bool = False` to `Settings` and pass it, matching `oneview_verify_tls`. | S |
| I-2 | `intersight/client.py:166` | `verify=False` unconditional, no setting. **Governed and deliberate** — ADR-0017's 2026-08-31 update, and `provider.py:285` logs a warning every run. Recorded here only because the ADR itself calls it "the first thing to revisit". Not a defect; do not change without asking. | none — track | — |
| I-3 | `openmanage/client.py:92-96`, `oneview/client.py:129-134` | Neither sets `limits=` or `follow_redirects=False` explicitly, unlike `intersight` (`:168`) and `redfish` (`:197,207`). httpx's default is already no-redirect, so this is documentation drift rather than a hole — but the four clients now have four different-looking postures for the same question. | One shared client-construction helper with an explicit posture per vendor. | S |
| I-4 | `openmanage/client.py`, `oneview/client.py` | **No retry/backoff at all** — no 429 handling, no transient-5xx retry. `intersight` has full jittered backoff honouring `Retry-After` (`client.py:457`), `redfish` retries transport failures only and never a 4xx (`client.py:456`). Two collectors abort a whole appliance on one blip. | Lift `intersight`'s backoff into shared code; apply to OME and OneView. Keep Redfish's never-retry-a-4xx rule intact — it is what stops account lockout. | M |
| I-5 | `openmanage/client.py:94`, `oneview/client.py:131` | Scalar `timeout=timeout_seconds` sets all four httpx timeouts to one value. `redfish` splits connect/read/write/pool (`:190`) and `intersight` splits connect/read (`:165`), both for stated reasons. Functionally fine; inconsistent. | Split, or state in a docstring why one value serves. | S |
| I-6 | `openmanage/*` | Log events are prefixed `ome.` while the `ManagerType` is `OPENMANAGE` and the module is `openmanage`. Every other provider's prefix matches its module. | Rename to `openmanage.*`, or record the intent. | S |
| I-7 | four providers | Run-summary lines share no key names (`servers_collected` vs `collected` vs `servers`; `hosts_failed` vs `unreadable_subresources`). Nothing can aggregate collector health across vendors. | One `collector.run_summary` event with fixed keys, emitted by the base class. | M |

### Maintainability

| # | File:line | Finding | Fix | Eff |
|---|---|---|---|---|
| M-1 | `openmanage/client.py:203-239` + `:26-30` | **Dead code.** `OmeClient.get_inventory` and `_DUMP_INVENTORY_ENV` (`INVENTORY_OME_DUMP_INVENTORY`) have **zero callers** — not in `backend/`, `tools/`, `tests/`, `docs/` or `deploy/`. ADR-0020 moved hardware to Redfish and deleted the mappers; this reader survived. | Delete both. The endpoint facts in its docstring are already in `docs/dell-collectors.md` — check before deleting, and move anything that is not. | S |
| M-2 | `credentials/env.py`, `redis/*`, `mongodb/*`, `logging/config.py`, `singleflight.py` | **57 of 384 definitions have no docstring, and every one is outside `providers/`.** Convention 8's claim about the providers **verifies**: all seven provider packages are conformant, with only three gaps (`oneview/client.py:endpoint` no `Returns:`, `openmanage/provider.py:collection_errors` no `Returns:`, `redfish/client.py:__aexit__` no `Args:`). Worst non-provider offenders: `mongodb/health_policy_repository.py` (8 of 8 missing), `manager_repository.py` (6 of 6), `site_repository.py` (6 of 6), `redis/client.py` (5 of 6), `redis/cache.py` (4 of 5). | Convert a file when you are already changing it, per convention 8 — not a sweep. Fix the three provider gaps now, they are one line each. | S each |
| M-3 | `singleflight.py:37` | `_inflight` is a module-global mutable dict with no reset hook and no loop affinity. Correct for one process with one loop; a test that runs two event loops, or a future multi-loop worker, shares state across them silently. Documented scope is per-process, which is honest — but the global has no owner. | Consider an instance owned by the app state, or document the loop assumption in the module docstring. | M |
| M-4 | `config/settings.py:178,182,191,232,241,267,308` | Every password / API key PEM is a plain `str`, not `pydantic.SecretStr`. `_drop_sensitive_keys` (`logging/config.py:36`) only removes **top-level event-dict keys** named `password`/`token`/…; a `settings=<repr>` field, a `model_dump()`, or a pydantic `ValidationError` echoing input would print the values straight through. `ManagerConnection` and `RedfishCredential` both redact their `__repr__` — `Settings` does not. | `SecretStr` for the seven credential fields, with `.get_secret_value()` at the two resolver call sites. | M |
| M-5 | `redfish/provider.py:577` | `logger.warning("redfish.resource_skipped", host=system.get("Id"), …)` — `host=` is given the *system's* `Id`, not the BMC host. Every other line in the file passes `target.host`. Confusing in a log grep, harmless otherwise. | Thread `target.host` through, or rename the key. | S |

### Performance

| # | File:line | Finding | Fix | Eff |
|---|---|---|---|---|
| P-1 | `oneview/provider.py:181-201` | The whole appliance is materialised — profiles + templates + `expand=all` hardware + every PSU map — before the first yield. At the documented 2500-server ceiling that is the largest resident set of any collector, and a kill at `activeDeadlineSeconds` loses the entire run. `redfish` and `ucs_central` both stream for exactly this reason. | Yield inside the `_matched` loop; fetch PSUs lazily per yielded server under the same semaphore. **Check ADR-0022 first** — the three-bulk-call shape is the decision, and streaming must not turn it into per-server calls. | M |
| P-2 | `ucs_central/provider.py:417-428` | `_collect_domain` buffers a whole domain's servers into a list before returning them. Streaming is per *domain*, not per server. Bounded by domain size (UCS caps a domain well below 10k), so this is a note, not a defect. | Leave. Recorded so the next reader does not mistake it for the streaming fix ADR-0014 describes. | — |
| P-3 | `mongodb/{manager,site,health_policy,classification_rule}_repository.py` | `find({}).to_list(length=None)` — unbounded reads. Each module's docstring justifies it (small human-curated collections). `managers` is now a *projection of config* (one row per `ManagerType`), so it is bounded by construction. No action. | none | — |

No N+1 pattern was found against Mongo or Redis inside `infrastructure/`.
`facet_breakdown` and `site_breakdown` are each one `$group` round trip with a
cardinality bounded by the enums, and both docstrings say why.

---

## 6. What is already good — specifically

Not a courtesy section; these are the things a refactor must not damage.

- **`_Joins` is the best expression of the `None` rule in the codebase.**
  `intersight/provider.py:162-201`. A failed *or budget-skipped* table stays
  `None` all the way to `ProviderServer`, and `for_server()` keeps
  "table unread" distinct from "this server has none" per server. Every other
  provider does this per field; this one does it structurally.

- **`redfish/targets.py` fails closed, before the network.** An unknown group
  name is an error rather than a fall-through to the default credential
  (`:314-323`, with the reasoning written down); a host embedding credentials
  is rejected rather than stripped (`:118-125`); a `verify_tls = false` without
  a written reason is refused (`:340-346`); a zero-host inventory is a distinct
  error from an all-hosts-down run (`:361-368`). `RedfishCredential.__repr__`
  redacts (`:40`), as does `ManagerConnection.__repr__`.

- **`validate_odata_id`** (`redfish/client.py:120`) — refuses to follow a link
  out of the BMC's own tree, *because* `X-Auth-Token` rides on every request.
  Paired with `follow_redirects=False` and a 3xx treated as an error
  (`:437-441`). That is a real, reasoned threat model, not a checkbox.

- **The shielded logout** (`redfish/client.py:249`). `asyncio.shield` around
  the session DELETE so a cancelled task still releases a session against a cap
  that "is often as low as 16". The `except asyncio.CancelledError` beside it
  is deliberate (teardown must never mask the cause) and worth keeping.

- **`_AuthGuard`** (`redfish/provider.py:57`) — two counters for two different
  estate shapes, and a docstring that says plainly it *bounds* damage rather
  than preventing lockout. Honest about its own limits.

- **`_guard_size`** (`redfish/client.py:504`) — checked after decompression,
  with the reason stated: a `Content-Length` check alone is defeated by gzip.

- **The `hs2019` signer** (`intersight/signing.py`) is pure and takes a clock,
  so the construction is reproducible against a fixed key — which matters
  exactly because a wrong signature fails as an indistinguishable 401. The
  algorithm is chosen by key type rather than by a library default, with the
  RSA-PSS trap written down (`:99-121`).

- **`_auth_message`** (`intersight/client.py:378`) distinguishes
  `iam_apikey_signature_invalid` (our bug) from
  `iam_apikey_authheader_invalid` (your credential) from clock skew — three
  different operator actions behind one HTTP status.

- **The OneView truncation check** (`client.py:352-369`): `nextPageUri` alone
  cannot tell truncation from completion, so `total` is compared and the
  mismatch logged at ERROR. The detection is right; only the reporting is
  missing (C-1).

- **PSU `Absent` handling and the `health` vocabulary** hold across all four
  reporting collectors, per CLAUDE.md's rule.

- **`singleflight.coalesce`** — the `future.exception()` call at `:66` (so
  asyncio does not log a spurious "never retrieved") and the identity check
  before `del` at `:76` are both the kind of detail that is only ever added
  after being bitten. The `# ty: ignore[invalid-return-type]` at `:53` is the
  correctly-spelled suppression convention 7 describes.

- **`redis/cache.py`** genuinely never raises — including the JSON decode path
  (`:77-85`), where a malformed payload is treated as a miss rather than
  surfaced as data.

- **`mongodb/audit_event_repository.py`** — no `update`/`delete` method exists,
  so immutability is structural, not a policy. The ISO-string cursor
  (`:130-141`) is built from the raw stored string rather than
  `datetime.isoformat()`, avoiding the `Z` vs `+00:00` mismatch — ADR-0006's
  bug, fixed at the source.

- **`ensure_indexes`** re-specifies rather than propagating
  `IndexKeySpecsConflict`, with the reversal reasoned in the module docstring.

- **`credentials/env.py`'s two-map split** — a login and an endpoint are
  separate questions, so `UCS_MANAGER` and `REDFISH_STANDALONE` can have one
  without the other, and the error names a variable that actually exists.

---

## 7. Questions for the user

Each of these is something no ADR settles, per the standing constraint.

**Q1 — OneView partial-success.** ADR-0022 says "One appliance failing is the
run failing; there is no partial-success accounting." That covers the appliance
being *down*. It does not obviously cover a run that **succeeded but saw a
third of the estate** — `oneview.collection_truncated` fires at ERROR and the
process still exits 0, which is the exact symptom the same ADR says to watch
for. Should `OneViewProvider` grow `collection_errors` so truncation and
unreadable PSUs produce exit 3 (PARTIAL), or is exiting 0 with an ERROR log the
intended behaviour? (C-1 is written assuming yes; it is not safe to assume.)

**Q2 — UCS per-domain deadline.** ADR-0014 explicitly defers a
`INVENTORY_UCS_CENTRAL_RUN_BUDGET_SECONDS`-shaped run budget as "a separate
decision". It does **not** discuss the narrower gap: `UcsManagerClient` imposes
no call deadline at all, unlike `UcsCentralClient._with_timeout`, so a single
wedged domain hangs its slot forever (C-5). Is the per-call deadline part of
the deferred decision, or a plain omission to fix now?

**Q3 — OME appliance TLS.** There is no `INVENTORY_OME_VERIFY_TLS`, so the OME
appliance connection is unverifiable-by-configuration while its BMCs and the
OneView appliance both have a setting (I-1). Is that a deliberate asymmetry, or
was the setting simply never added when `verify_tls` was given a parameter?

**Q4 — `OmeClient.get_inventory`.** Zero callers anywhere since ADR-0020 (M-1).
Delete, or is it kept deliberately as a field-discovery aid for the next Dell
investigation? If kept, it wants a docstring line saying so.

**Q5 — provider-type naming.** `ome.*` log events vs `OPENMANAGE` /
`openmanage` everywhere else (I-6). Rename, or is `ome` the term operators
actually use?
