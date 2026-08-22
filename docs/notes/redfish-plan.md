# Redfish collector — Phase 2 plan

Synthesis of five independent agent positions (architect, inventory +
credentials, security, ops, red team) over the research in
`redfish-phase0.md` and `redfish-domain.md`. No implementation code
exists.

Where the agents agreed independently, I treat it as settled — separate
agents reaching the same answer from different reasoning is the strongest
signal available without hardware. Where they disagreed, the disagreement
is surfaced in §5 rather than resolved silently.

**Every claim below about existing repository behaviour was verified
first-hand**, not taken from an agent report. The three pre-existing bugs
in §1 were each reproduced or confirmed by direct inspection.

---

## 1. Three pre-existing bugs, found while designing this

These are in shipped code. None is caused by this feature; all three
block or degrade it, and two are trivially fixable. **They are separate
commits, landing before the collector.**

### 1.1 `uniq_system_uuid` rejects the second server with no UUID — VERIFIED LIVE

`mongodb/indexes.py:47` declares:

```python
partialFilterExpression={"identity.system_uuid": {"$exists": True}}
```

MongoDB's `$exists: true` **matches a field that is present and null**,
and `model_dump(mode="json")` always emits the key. Reproduced against
live Mongo using the repo's own `SERVER_INDEXES`:

```
null-uuid #1:    INSERTED
null-uuid #2:    *** DuplicateKeyError *** index: uniq_system_uuid
empty-serial #1: INSERTED
empty-serial #2: INSERTED
```

So **exactly one UUID-less server can exist in the entire inventory.**
Every subsequent one raises `DuplicateKeyError`; `_ingest_one`'s recovery
path looks up by `(vendor, serial)`, finds nothing, and re-raises into
`IngestSummary.errors` — every run, forever. Which host wins depends on
completion order, so it can change between runs.

`ComputerSystem.UUID` is schema-optional (`redfish-domain.md` §1b), so
OpenBMC whiteboxes and older firmware hit this immediately. It has never
fired because UCS always reports a UUID and the fake generator always
sets one; no test covers two servers with `system_uuid=None`.

**Fix:** `{"$type": "string"}` — an allowed partial-filter operator, the
same family as the `$gt: ""` trick already used on the serial index
directly below it. `ensure_indexes` raises `OperationFailure` on spec
drift, so a deployed environment needs the old index dropped, not just
redeclared. **Test:** two servers, both `system_uuid=None`, both ingest.

### 1.2 `run_collector` never writes the `Manager` document

`IngestService.ingest` upserts managers only from its `managers=`
argument. `tools/seed_inventory.py:98` passes it;
`tools/run_collector.py:440` does not.

So `manager_for()`'s docstring ("It is still written to the `managers`
collection on each run") and `CLAUDE.md`'s "A `Manager` document is still
written on each run, but it is a *projection*" are **both false today** —
every UCS-collected server carries `manager_id="mgr_ucs_central"`
pointing at a document that does not exist.

**Fix:** one keyword argument. **Test:** after a collector run the
manager document exists and `Server.manager_id` resolves to it.

### 1.3 `--manager-type UCS_MANAGER` never reaches its own error message

`_run` calls `credential_resolver.resolve(manager_type)` unconditionally,
and `EnvConnectionResolver.resolve` raises for `UCS_MANAGER` — so the
carefully-worded `NotImplementedError` in `_build_provider` explaining
the Central path is **dead code**. Proof nobody has exercised the path
this collector is about to take.

Relevant because `REDFISH_STANDALONE` has the same shape (a login with no
endpoint) and would hit the same wall. Fixed as part of §3.6.

---

## 2. The settled design

Independently agreed by two or more agents unless noted.

### 2.1 Shape

Four modules under `backend/app/infrastructure/providers/redfish/`,
mirroring `ucs_manager`'s split:

| Module | Holds |
|---|---|
| `client.py` | `RedfishClient` — one host, one session, `async with`; exception hierarchy; TLS/timeout/retry; `$expand` probe |
| `mapping.py` | pure `dict -> ProviderServer`; no I/O, no `await` |
| `targets.py` | inventory parsing and credential resolution; no network |
| `provider.py` | fan-out, budgets, `collection_errors`, the circuit breaker |

**Nothing is shared with `ucs_common`** — it is DN-string structure and
Cisco SDK presence semantics, none of which Redfish has. Unifying
`is_equipped` with `Status.State == "Absent"` would produce a function
with a vendor branch in it, the opposite of that module's purpose.

### 2.2 Configuration: a login with no endpoint

`_LOGIN_FIELDS[REDFISH_STANDALONE] = ("redfish_username", "redfish_password")`,
and **deliberately no `_ENDPOINT_FIELD` entry** — the same carve-out
`UCS_MANAGER` already uses and which `credentials/env.py`'s two-map split
exists to express. The fleet comes from a mounted inventory file.

A homogeneous fleet therefore needs **two environment variables and a
list of hosts**, reusing `resolve_login()` unchanged.

Per-host credential overrides are referenced **by name**, not by host: a
host entry says `credential = "site-one"`, and a separately-mounted
Secret maps names to values. Keying by host would mean N identical Secret
keys for a homogeneous fleet, and renumbering a BMC would orphan its
credential. This is what `Manager.bmc_credential_ref` reserved — "a name
rather than a value".

### 2.3 Auth: Redfish session, not Basic

Three independent arguments landed on the same answer: Basic makes every
one of ~25 GETs a login event (25× the lockout pressure and, on an
LDAP-backed account, 25 directory round trips per server); Basic puts a
*reusable plaintext password* on every request where session auth puts a
short-lived token on all but one; and the circuit breaker's "N distinct
hosts" arithmetic only maps onto what the BMC counts if there is one auth
event per host.

Session leak — Basic's genuine advantage — is closed by `async with`,
**except under cancellation** (see §3.3).

### 2.4 Safety invariants

Two agents converged on the first from different directions:

> **Never authenticate to a host that has not already answered
> `/redfish/v1` with a valid Redfish ServiceRoot over a verified TLS
> connection.**

ServiceRoot is unauthenticated by specification, so this costs one GET.
A typo'd address that is not a BMC fails the probe; one that is not ours
fails certificate verification. Neither ever receives a password. This
closes the highest-consequence failure in the feature.

- **A default credential is only ever presented to a host in the
  inventory.** Never to an address found by scanning. Enforced
  structurally: the collector contains no discovery code path at all.
- **Reject any `@odata.id` that is not a relative path under
  `/redfish/v1/`, and never follow redirects.** Both retarget the next
  request, and `X-Auth-Token` rides on every request — httpx strips
  `Authorization` cross-origin but cannot know a custom header is
  sensitive.
- **Never retry a 401.** A 401 is configuration, not a transient fault.
- **A resource 403 is not an auth failure.** Mandating a ReadOnly BMC
  role guarantees 403s on some vendors; if those feed the breaker, every
  run on a correctly-configured estate aborts.
- **Mandate a Redfish `ReadOnly` role.** The only control that reduces
  the *consequence* of a leak rather than its probability, and free
  because the collector genuinely only reads.

### 2.5 Pacing

| Setting | Default | Why |
|---|---|---|
| connect timeout | 10 s | fail an unreachable host fast; 400 dead hosts is pure wasted budget |
| read timeout | 30 s | ~30 iLOs failed fleet-wide at 3 s and **20 s fixed it**; 30 s adds headroom |
| per-host wall-clock budget | 180 s | **the knob that does not exist today.** 25 requests × a 30 s read timeout is 750 s of worker time for one wedged host |
| run budget (in-process) | 3600 s | must trip **before** `activeDeadlineSeconds` (3900) — ADR-0014 names the hard kill "with no logged reason" as the failure it was avoiding |
| fleet concurrency | 16 | cost is per-host and hosts are independent |
| per-host concurrency | 1 | `MultipleHTTPRequests` (Redfish 2022.1) is the only standard signal; **absence reads as "serialize"** |

Retries are deliberately **not** configurable — a knob is what produced
the DMTF library's `max_retry=10` with a flat 1 s sleep.

### 2.6 Traversal

ServiceRoot → **conformance gate** → session → Systems collection
(follow `Members@odata.nextLink`; never read `Members@odata.count`) →
per System: Processors filtered by `ProcessorType`, Storage's inline
`Drives[]` links unioned with `Chassis/Drives` and deduped by
`@odata.id`, EthernetInterfaces, and `Links.ManagedBy` → Manager →
its EthernetInterfaces for `bmc_mac` (fetched once per BMC).

`$expand` is probed, **verified with one real expanded GET**, cached
in-process, and always has the N+1 fallback. The member parser
`member if "@odata.type" in member else await get(member["@odata.id"])`
makes expand-on and expand-off the same code path *and* handles a
legally link-only member inside an expanded collection.

**The conformance gate is what makes "iLO 4 is out of scope" true rather
than aspirational.** A pre-Redfish dotted `@odata.type` or an absent
`RedfishVersion` raises before a credential is sent. Stated
vendor-neutrally, so any equally divergent BMC fails the same way.

Skipped: `LogServices` (~10-minute scrapes), `Memory` (the summary
already has the total), `PCIeDevices`/`GraphicsControllers` (neither
carries GPU memory), `SimpleStorage` (double-count trap).

### 2.7 Identity and streaming

`external_id = redfish://{host}{system @odata.id}` — unique over
`(bmc, system)`, which OpenBMC multi-host requires. Re-addressing a BMC
changes it harmlessly, because correlation is `(vendor, serial)`.
**`external_id` is not a correlation key and never has been.**

`list_servers()` yields each host's servers as that host completes
(`as_completed`), never `gather`s the fleet — so a killed run has already
persisted what finished. Host order is **shuffled per run** (§3.5).

---

## 3. What the red team broke, and the fixes

### 3.1 A partial read silently heals a real fault — the worst finding

`_build_server` carries forward only `classification`, `health`,
`maintenance`, `openshift`, and rebuilds `hardware`, `network`,
`identity.nic_macs`, `connectivity`, `name` and `model` from the
`ProviderServer` every run. The repository does `replace_one(upsert=True)`.

So a host whose `Storage` collection 404s — which sushy 5.10.0 had to
handle because HGX boards do exactly this — yields
`storage_total_bytes=0, storage_drives=()`, **overwriting good data with
zeros**. `health/facts.py` derives `storage.failed_drive_count` from that
list, and the seeded `storage.failed_drive` policy fires on `>= 1`. Zero
drives means zero failed drives.

**A server with a genuinely failed disk flips from CRITICAL to HEALTHY
because we could not read its Storage collection** — and
`_emit_transition_events` writes a `HEALTH_STATUS_CHANGED` audit event
asserting the recovery. `last_seen_at` is still stamped fresh, so the
staleness gauge reports the hollowed-out document as healthy: **the
design's only observability mechanism is structurally blind to the exact
failure it ships alongside.**

Same shape: EthernetInterfaces 404 wipes `nic_macs` and `bmc_mac` (the
slice-2 identity ladder's inputs); a null `MemorySummary` zeroes memory;
a Processors 404 zeroes CPU.

**The fix is at the port, not in the collector.** `ProviderServer`'s
`int = 0` defaults cannot express "unknown", so §1b's carefully-derived
null-vs-absent rule dies at the DTO boundary. Required:

1. `memory_total_bytes`, `cpu_sockets`, `cpu_cores`, `cpu_threads`,
   `storage_total_bytes` become `int | None`, and `storage_drives` /
   `nic_macs` gain a way to say "not read" distinct from "empty".
2. `_build_server` carries the previous value forward when a field is
   `None`, exactly as it already does for `classification`/`health`.
3. Existing providers are unaffected — they always supply real values.

**Test:** ingest a full server, then re-ingest the same server with
storage unreadable; assert `storage_total_bytes` and the CRITICAL health
state both survive, and that no `HEALTH_STATUS_CHANGED` event is written.

### 3.2 The vendor flip fires in the direction we chose

`redfish-domain.md` works the B300 case through and concludes
manufacturer-mapping keeps identity stable. It does — for the
unmanaged→managed transition. It introduces the mirror-image bug.

`Manufacturer` is `["string","null"]` and schema-optional, and §9.6.1
says null means "supported but unknown **this cycle**". One bad cycle on
a Dell box yields `vendor=standalone` → correlation key changes →
**a new document**, orphaned permanently, counted in every per-vendor
total and site rollup forever. `uniq_vendor_serial` cannot catch it
because the vendor genuinely differs — the exact argument used *against*
blanket-`standalone`, now firing the other way.

**Fix:** `Manufacturer` is load-bearing for identity, so a null or absent
`Manufacturer` is a **collection failure for that host**, not a vendor
decision. Do not emit the `ProviderServer`; record it in
`collection_errors`. `Vendor.STANDALONE` remains for a manufacturer that
is *present and unrecognized*.

**Test:** a host whose `Manufacturer` is null is not ingested and appears
in `collection_errors`; a host reporting `"Lenovo"` ingests as
`standalone`.

### 3.3 `async with` does not guarantee logout under cancellation

When the run budget fires, pending tasks receive `CancelledError`. The
session context manager's `__aexit__` then does
`await client.delete(session_location)` — which **raises `CancelledError`
immediately**. The DELETE never goes out.

So the stated safety property holds on the normal path and fails on
exactly the path the run budget uses. ~16 leaked sessions per trip,
against caps as low as 16; sessions expire only on inactivity. Next run's
session-create returns 401/503 on those hosts, **indistinguishable from a
bad password** → the breaker trips → the whole fleet goes uncollected
because the previous run was slow.

**Fix:** `asyncio.shield` the logout with its own short timeout in a
`finally`, and make the run-budget path *drain* rather than cancel.
**Test:** cancel a task mid-traversal; assert the DELETE was still issued.

### 3.4 The circuit breaker's effective threshold is concurrency, not N

With 16 hosts in flight, 16 logins are dispatched before the first 401
returns. **The 3-vs-5 dispute is noise; the effective threshold is 16.**
Worse, N=3 is already *past* Dell's documented 3-failures-in-60s block,
so the breaker cannot prevent the one lockout mechanism with a cited
default — only the **pre-flight** can.

Three more failure modes: per-host credentials give 400 counters each
sitting at 1/N while accounts lock one at a time (needs a
credential-agnostic **total 401 budget** per run); an idle session
expiring produces a resource-401 byte-identical to a bad-credential 401
(**count only 401s from the session-create POST**); and the breaker
**resets every cron tick**, so a bad credential repeats the damage every
6 hours forever (needs a persisted marker that refuses the next run).

**The ADR must state the breaker as a backstop that bounds damage, not as
prevention.** The pre-flight is the mechanism that prevents.

### 3.5 What breaks at 500 hosts

- **Ordering bug:** if the per-host budget wraps the semaphore
  acquisition rather than sitting inside it, queue wait counts against
  the budget and every host past the first few "times out" without a
  packet sent. **Semaphore first, budget after.**
- **The arithmetic does not close:** 3600 s × 16 ÷ 180 s = **320 hosts**
  worst case. Nothing checks `run_budget >= hosts / concurrency ×
  host_budget`, so the run budget silently truncates a fleet it was never
  sized for. Validate at load and say so.
- **Truncation is systematically biased:** `as_completed` delivers fast
  hosts first, so the budget kills **the same slow hosts every run** —
  permanently stale, invisibly. **Shuffle host order per run.**
- **Construct the client inside the semaphore**, or 500 live TLS
  contexts and connection pools exist at once.
- **`Connection: close` may be miscited.** sushy's comment is about
  *long-running persistent* connections; a 25-request burst is not
  obviously that case, and forcing a full TCP+TLS handshake 12,500 times
  per run is a real multiplier. Measure before paying for it.

### 3.6 `--manager-type REDFISH_STANDALONE` cannot reach the provider

`_run` calls `resolve()` unconditionally and `_build_provider` calls it
again; with no `_ENDPOINT_FIELD` entry both raise, giving exit 2 with a
message naming no variable. Needs an explicit branch in both, mirroring
the existing `if manager_type is ManagerType.UCS_CENTRAL:` pre-flight.

`manager_for()` builds `Manager(endpoint=connection.endpoint)`; the
inventory file path is the most informative value available.

### 3.7 `--dry-run` bypasses the pre-flight

`_dry_run_one_manager` never calls `health_check()`, by design. So a
pre-flight living there means **`--dry-run` fans out to every BMC with a
cold breaker** — and `--dry-run` is exactly what an operator reaches for
when they suspect the password is wrong.

But putting it in `health_check()` has the mirror-image failure: it
raises → `ingest` propagates → exit 1 → the entire fleet uncollected
because one probe host was in maintenance.

**Fix:** neither. The pre-flight is a provider-internal step at the head
of `list_servers()`, with the probe host chosen from *reachable* hosts
rather than the first line of the file. `health_check()` validates
configuration only and makes no network call.

### 3.8 Serial collisions collapse the fleet, silently, exit 0

SMBIOS placeholders (`"To be filled by O.E.M."`, `"Default string"`,
`"0123456789"`) leak into Redfish `SerialNumber` on whitebox hardware.
`normalize_text` maps them all to one string; combined with
`vendor=standalone` they share a correlation key. **50 whitebox servers
become one document**, each overwriting the last, printing
`fetched=50 created=1 updated=49` and **exit 0**.

And a host with *no* serial creates a **new document every run, forever**
— there is no delete path anywhere in this codebase.

**Fix:** a placeholder-serial denylist in the mapping, treated as "no
serial"; and refuse to ingest a server with neither a serial nor a UUID,
recording it in `collection_errors`. **Test:** two hosts sharing
`"Default string"` produce two `collection_errors`, not one document.

### 3.9 Exit 3 + `backoffLimit: 0` destroys the signal

Exit 3 (PARTIAL) is the *normal* outcome for 400 independent BMCs, and
with `backoffLimit: 0` every such run is a Failed Job. Within a week the
alert is muted, and the day exit 1 happens it is indistinguishable.
§1.1 guarantees `summary.errors > 0` permanently on any fleet with a
UUID-less host, so this is not hypothetical.

Also: a ConfigMap mount lagging at pod start reads as "inventory absent"
→ exit 2 → no retry → a full cycle of the fleet lost. UCS never had this
exposure because its config was env-only.

**This is decision D4 in §5** — I do not think I should settle it alone.

### 3.10 `collection_errors` bypasses every secret scrubber

`_drop_sensitive_keys` is a **structlog processor**; `run_collector`
prints `collection_errors` with `print()`. The UCS precedent formats them
as `f"...: {exc}"` — a raw exception string.

An inventory entry carrying userinfo (`https://user:pass@host`) reaches
`bmc_address_raw`, which `parse_bmc_address` preserves **verbatim** while
`urlsplit` silently drops userinfo from `.hostname` — so the password
lands in the MongoDB document, the detail API, and `--dry-run` output,
while the parsed fields sitting beside it look clean.

**Fix:** reject userinfo in an inventory entry at parse time; compose
`bmc_address_raw` from components, never from the operator's string;
route `collection_errors` construction through a redactor. **Test:**
assert a password sentinel appears in no document, no log line, and no
`--dry-run` output.

---

## 4. Task breakdown, in dependency order

Each task names its test. Tasks 1–3 are separate commits landing before
the collector.

| # | Task | Test |
|---|---|---|
| 1 | `uniq_system_uuid` → `{"$type": "string"}` | two servers with `system_uuid=None` both ingest |
| 2 | `run_collector` passes `managers=` to `ingest` | after a run the manager document exists |
| 3 | Nullable numeric fields on `ProviderServer` + carry-forward in `_build_server` | a partial re-ingest preserves storage and the CRITICAL state, and writes no health-transition event |
| 4 | `ManagerType.REDFISH_STANDALONE` + `_LOGIN_FIELDS` + `_run`/`_build_provider` branches | `--manager-type REDFISH_STANDALONE` with no config exits 2 naming the variables |
| 5 | `targets.py` — inventory parse, credential resolution, load-time validation | zero hosts / unknown group / userinfo / duplicate host each fail at load with a named message |
| 6 | `client.py` — session lifecycle, timeouts, TLS, retry taxonomy, `@odata.id` validation | session cleanup on success *and* on cancellation; a 401 is never retried; an off-host `@odata.id` is rejected |
| 7 | `mapping.py` — the §1b property mapping | NVMe→`NVME` via `Protocol`; GPU MiB→bytes; `Status.Health`→severity; missing optional properties |
| 8 | `provider.py` — fan-out, budgets, breaker, pre-flight, shuffle | partial-fleet run; auth-rejected host; unreachable host; budget-exceeded host |
| 9 | CI fixture — the ~100-line stdlib server from `sushy-static`'s skeleton + session auth + fault injection | the eight scenarios in the brief, hermetically |
| 10 | `run_collector` integration — `--debug-http`, dry-run `collection_errors`, aggregated output | `--debug-http` leaks no sentinel; a dry run reports failed hosts |
| 11 | `source_provider` filter — `FILTER_FIELDS`, index, list schema, frontend | filter returns only Redfish servers; unknown value still raises |
| 12 | Helm — CronJob, values, inventory ConfigMap, credentials Secret | `helm template` renders; no secret in the rendered pod spec |
| 13 | `tools/verify_redfish` — the single-host probe | GOOD/PARTIAL/BAD verdicts against the fixture |
| 14 | Docs — ADR-0016, `docs/redfish-collector.md`, `docs/test-redfish-collector.md`, README, architecture.md | — |

---

## 5. Decisions I want from you

**D1 — Inventory file format.** Three agents proposed three formats.
TOML (stdlib `tomllib`, comments, groups/named credentials, verified that
`pyyaml` is only a transitive dependency), plain text `host key=value`
(~12 lines of parsing), or YAML (most familiar to a k8s operator, but
needs `pyyaml` promoted to a direct pin and a new mirror entry).
**My recommendation: TOML.** Zero new dependency, comments matter for a
file recording `verify_tls = false  # INC-1234`, and groups keep a
400-host file maintainable.

**D2 — Per-host credentials now, or global-only in v1?** The full
named-credential chain is designed and costs ~15 lines more than
global-only. **My recommendation: build it now** — the chain's *load-time
validation* (unknown group name, undefined credential) is what prevents a
typo from spraying the default credential, and that value exists even
with one credential configured.

**D3 — Circuit breaker threshold.** Given §3.4 shows the effective
threshold is concurrency (16), the honest options are: keep N=3 as a
backstop and rely on the pre-flight; or add a **credential-agnostic total
401 budget** per run. **My recommendation: both, and state in the ADR
that the breaker bounds damage rather than preventing lockout.**

**D4 — Exit 3 and `backoffLimit`.** With PARTIAL as the normal outcome,
either the Job is permanently red and Job-status alerting is worthless
(and staleness alerting is the only signal), or exit 0 below a
configurable failure ratio makes Job status meaningful but can turn real
failures green. **My recommendation: keep exit 3 meaningful, set
`backoffLimit: 1` (not 0) to survive a lagging ConfigMap mount, and
alert exclusively on staleness — never on Job failure.** This differs
from the ops position and I want your call.

**D5 — `Accept-Encoding: identity`?** Proposed as a one-line
decompression-bomb defence, but §1d found some BMCs reject the header.
**My recommendation: drop it**; bound the response with a streaming byte
cap instead, which costs no compatibility.

**D6 — Does the staleness work land in this slice?** It is ~100 lines in
the API process and it is the **only** answer to "40 hosts have been
failing for two weeks", because collector metrics are unscrapeable
(verified: `/metrics` is served only by the API; a CronJob pod is never
scraped). **My recommendation: ship the collector with the mongosh query
in the runbook, and schedule the gauges as a follow-up** — but say
plainly in the ADR that staleness detection is manual until then.
