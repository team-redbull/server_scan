# 16. Redfish collector for standalone servers

Date: 2026-08-23

## Status

**Accepted, not yet implemented.** No collector code exists. This records
the design, the evidence behind it, and what it cannot yet claim. The six
open questions it was reviewed against were settled on 2026-08-23 and are
folded in below; `docs/notes/redfish-plan.md` §6 has the reasoning for
each.

The three defects in "Pre-existing defects this surfaced" were in shipped
code and are **fixed** — commits `1b2c10b`, `acca277`, `d8cdda2`.

## Context

Every collector this platform has points at an *aggregator*. UCS Central
is asked what exists and answers with 152 registered domains and their
addresses (ADR-0014); the collector fans out from there. `credentials/
env.py` encodes that model as a fact of the type system: one endpoint and
one login per `ManagerType`.

A growing set of machines has no aggregator. They are not registered with
UCS Manager, UCS Central, Intersight, OneView or OpenManage Enterprise,
and the operator's own example is a Cisco server whose CIMC speaks
Redfish but which Intersight cannot yet manage. The requirement, stated
directly: **any BMC that speaks conformant Redfish should be
collectable.**

That is not a smaller version of the existing problem. It inverts three
of its properties:

1. **Nothing enumerates the fleet.** A BMC knows about itself. The host
   list must come from something we own — the central new problem, and
   not a Redfish problem at all, since Redfish begins working only once
   an address is already known.
2. **Cost is per server, not per manager.** ADR-0014 could say "scale was
   not a factor" because a domain costs ~11 round trips whether it holds
   10 servers or 500. Here each host costs ~25 round trips against
   embedded hardware that degrades under polling. Bounded concurrency,
   per-host timeouts and a total-run budget stop being tuning knobs and
   become correctness requirements.
3. **Failure is normal.** With 400 independent devices some are always
   down. A run where 40 fail is a Tuesday, not an incident.

And it introduces a failure mode with no precedent in this codebase:
**account lockout**. Many BMCs lock an account after a few failed logins,
so a retry loop with a wrong password is actively destructive against the
estate's own management plane.

## Evidence

Held to ADR-0009's bar: confirm against the primary artefact, not a
documentation summary. The full research is in `docs/notes/
redfish-domain.md`; the load-bearing items were re-verified first-hand
against the downloaded DMTF schema bundle (2026.1) and DSP0266 1.14.0.

**On the schema.** Every resource's `required` list is only
`@odata.id`, `@odata.type`, `Id`, `Name` (plus `ChassisType` on Chassis).
Everything this collector reads is schema-optional, and §9.6.1
distinguishes *absent* ("not supported by this implementation") from
*null* ("supported, but unknown at the time of the operation") — a
distinction that turns out to decide a correctness bug (below).

Four findings changed the mapping:

- `ProcessorSummary` counts **central** processors and excludes GPUs.
  DMTF's own mockup reports `Members@odata.count: 10` against
  `ProcessorSummary.Count: 8`.
- `ProcessorSummary.CoreCount` is declared in
  `Namespace="ComputerSystem.v1_14_0"`, whose `Redfish.Release` is
  **2020.4** — absent on 2016–2019 firmware, so summing
  `Processor.TotalCores` is mandatory rather than defensive.
- `Drive.MediaType` is exactly `["HDD","SSD","SMR"]`; **NVMe is expressed
  through the separate `Protocol` property.** Reading only `MediaType`
  would report every NVMe drive in the fleet as an SSD.
- GPU memory is `Processor.MemorySummary.TotalMemorySizeMiB` — **MiB**,
  against `ComputerSystem.MemorySummary.TotalSystemMemoryGiB` in **GiB**.

**On real hardware.** iLO 7 before 1.22 *advertises* `$expand` and
returns HTTP 400, so `ProtocolFeaturesSupported` is a hint and never a
contract. ~30 iLOs failed fleet-wide at a 3-second timeout and 20 seconds
fixed it. `LogServices` scrapes have been measured at ~10 minutes.
Lockout defaults differ in kind, not degree: Dell blocks the **source
IP** (3 failures / 60 s → 1 hour), Lenovo XCC locks the **account** and
can be configured to hold it until an admin intervenes, HPE merely
delays.

**On the client library.** `DMTF/python-redfish-library` hardcodes
`verify = False` inside its request loop, overridable only by supplying a
CA file, and calls `disable_warnings()` process-wide at import; it
defaults to no timeout and 10 retries, and logs response headers
unredacted — where `X-Auth-Token` lives. `sushy` is better engineered
(`verify=True`, split timeouts, never retries `SSLError`) but is
sync-only and untyped. Both were read from source, not documentation.

**On testing without hardware.** `sushy-tools` was installed and run:
**it has no `SessionService` at all** — zero matches for
`sessionservice`/`x-auth-token` in the package, and its ServiceRoot
template has no `Links` block. DMTF's own mockup server is not on PyPI,
is stale, and also has no auth. Neither can exercise session login,
the only non-trivial part of the client.

## Decision

Build `app.infrastructure.providers.redfish` as a peer of `ucs_manager`,
registered in `_PROVIDER_FACTORIES` with its own CronJob.

**The runtime client is ours, on the already-pinned `httpx==0.28.1`.**
No new dependency, native async, `py.typed` under `mypy --strict`, and
zero new air-gap mirror entries. The knowledge is carried over rather
than the code: `Connection: close` (sushy's field studies), split
connect/read timeouts, never retrying an `SSLError`, and redacting
`X-Auth-Token` in *response* logging — the precise place the DMTF library
leaks it. Revisit sushy if vendor-specific branches pass ~5.

**Configuration is a login with no endpoint** — the same carve-out
`UCS_MANAGER` uses, and the reason `credentials/env.py` has two maps
rather than one. The fleet comes from a **TOML** inventory file mounted
from a ConfigMap, parsed with stdlib `tomllib` so it adds no dependency
and no air-gap mirror entry; per-host credential overrides are referenced
**by name** from a Secret. A homogeneous fleet therefore needs two
environment variables and a list of hosts.

TOML over YAML because `pyyaml` reaches this project only transitively
via `uvicorn[standard]`, so using it properly would mean a direct pin and
regenerated lock files. TOML over a bare list because the file needs
**comments** — `verify_tls = false  # INC-1234` is how a relaxed security
control stays reviewable — and **grouping**, so a per-site credential is
expressible without inventing syntax.

The credential resolution chain (host → group → defaults → the
fleet-wide login) is built in the first slice rather than deferred. Its
value is not exotic hardware support: it is **load-time validation**. A
typo'd group name fails the run naming the known groups, instead of
silently falling through to the default service account and presenting it
to a machine it was never meant for.

**This deviates from ADR-0012's "no secret volume to mount", and the
deviation is named rather than slipped in.** That statement was about
per-manager credential *directories* alongside `Manager` documents, whose
harm was a second source of truth. A host list is neither secret nor a
second source of truth — it is data, hundreds of lines, changing when the
estate changes rather than when the deployment does.

**Authentication is Redfish session auth**, with `logout()` guaranteed by
`async with` *and* shielded against cancellation. Basic is
spec-guaranteed and stateless, but makes every one of ~25 GETs a login
event — 25× the lockout pressure when a credential is wrong, and on an
LDAP-backed account 25 directory round trips per server.

**Two safety invariants bound the blast radius:**

> Never authenticate to a host that has not already answered
> `/redfish/v1` with a valid Redfish ServiceRoot over a verified TLS
> connection.

> A credential is only ever presented to a host listed in the inventory.
> Never to an address found by scanning.

The first costs one GET (ServiceRoot is unauthenticated by
specification) and means a typo'd address that is not a BMC fails the
probe while one that is not ours fails certificate verification —
neither ever receives a password. The second is enforced structurally:
the collector contains no discovery code path, and discovery, if ever
built, is a separate program that cannot present a credential.

**Vendor is the manufacturer, mapped.** `ComputerSystem.Manufacturer` →
`dell`/`cisco`/`hp` where recognized, `Vendor.STANDALONE` where present
but unrecognized. "Has no manager" is carried by `source_provider`, not
by `vendor`, because `IngestService` correlates on
`(vendor, serial_normalized)` — putting management state into the vendor
field means a machine splits into two documents the day it gains a
manager. A **null or absent** `Manufacturer` is a collection failure for
that host, not a vendor decision, for the mirror-image reason.

**`INVENTORY_COLLECTOR_NAME_PATTERN` does not apply.** The pattern exists
because a vendor manager holds the whole datacenter and the name is the
only discriminator. Here the inventory list *is* the filter, and a more
precise one. Applying `^ocp` over a name a BMC does not know would
discard every host the operator deliberately listed.

**`attachments` is empty and `gpus` is best-effort.** A standalone server
has no fabric interconnect; emitting NIC link state as pseudo-attachments
would make the seeded `connectivity.fabric_paths_down` policies evaluate
against fiction. An empty `gpus` tuple means "not discoverable here",
never "none installed".

**The CI fixture is ours too** — ~100 lines built on `sushy-static`'s
118-line Apache-2.0 stdlib skeleton, plus session auth, expiry, and a
`path → (status, delay)` fault-injection table. That combination tests
what no off-the-shelf option can: a 401 without a token, a session
expiring mid-run, a 500 on one collection member, and a deliberately hung
request.

## Pre-existing defects this surfaced

Found while designing, verified first-hand, not caused by this feature.
Fixed as separate commits ahead of the collector.

**`uniq_system_uuid` rejects the second server with no UUID.**
`partialFilterExpression={"identity.system_uuid": {"$exists": True}}`
matches a field present *with a null value*, and `model_dump(mode="json")`
always emits the key. Reproduced against live MongoDB with the repo's own
`SERVER_INDEXES`: the second null-UUID insert raises `DuplicateKeyError`,
`_ingest_one`'s recovery finds nothing by serial and re-raises, and the
server fails ingest on every run forever. `ComputerSystem.UUID` is
schema-optional, so this blocks OpenBMC whiteboxes and older firmware
outright. It has never fired because UCS always reports a UUID and no
test covers two servers without one. Fix: `{"$type": "string"}`.

**`run_collector` never writes the `Manager` document.**
`IngestService.ingest` upserts managers only from its `managers=`
argument; `seed_inventory.py` passes it and `run_collector.py` does not.
So `manager_for()`'s docstring and `CLAUDE.md` both describe behaviour
that does not happen — every UCS-collected server's `manager_id` points
at a document that does not exist.

**A partial read silently heals a real fault.** `_build_server` carries
forward only `classification`, `health`, `maintenance` and `openshift`,
and rebuilds `hardware` from the `ProviderServer` every run, into a
`replace_one(upsert=True)`. A host whose `Storage` collection 404s —
which sushy 5.10.0 had to handle because HGX boards do exactly this —
yields zeros that overwrite good data. `storage.failed_drive_count`
becomes 0, the seeded `storage.failed_drive` policy stops firing, and
**a server with a genuinely failed disk flips from CRITICAL to
HEALTHY** — with a `HEALTH_STATUS_CHANGED` audit event asserting the
recovery, and `last_seen_at` stamped fresh so staleness monitoring reads
the hollowed-out document as healthy.

This is the same class as ADR-0009's fabric-path defect: the run
succeeds, the numbers are plausible, and a health signal silently
switches off. The fix is at the port — `ProviderServer`'s `int = 0`
defaults cannot express "unknown", so the schema's own absent-vs-null
distinction dies at the DTO boundary. The numeric fields become
`int | None` and `_build_server` carries the previous value forward,
exactly as it already does for classification and health.

## What is still unproven

Held to ADR-0009's standard: what a live run has *not* settled.

**No Redfish hardware has been touched.** Everything rests on the DMTF
schema, DMTF's own mockups, and vendor documentation. ADR-0009 found five
real defects that were invisible without hardware — a nonexistent MO
class, a BMC filter matching nothing, a whole class of adapter interface
never collected, fabric counts always zero, and servers named after their
chassis slot. **Assume this design has an equivalent set.**

Specifically unsettled:

- **Whether Dell iDRAC or HPE iLO populate `Processors` with
  `ProcessorType: "GPU"`** for arbitrary add-in GPUs. The path is
  standard and Lenovo XCC implements it; no evidence was found for the
  two vendors actually in scope. `gpus` therefore ships best-effort.
- **Whether the fleet's firmware honours `$expand`.** Probed and verified
  per host at runtime, so being wrong costs latency and never
  correctness — but the ratio is unknown.
- **Whether `Accept-Encoding: identity` is safe.** Proposed as a
  decompression-bomb defence; the research found some BMCs reject it.
- **Whether the string `"N/A"` ever appears in a numerically-typed
  field.** Widely assumed, including in this project's own earlier
  notes; **no primary citation was found.** What *is* confirmed is `null`
  where a number is expected, and keys absent entirely. Coded for
  defensively, but not claimed as established.
- **The per-host request count (~25) is derived from the mapping, not
  measured.** Every concurrency and budget number scales linearly with
  it.
- **The circuit breaker cannot prevent Dell's IP block.** With 16 hosts
  in flight, 16 logins are dispatched before the first 401 returns, so
  the effective threshold is concurrency rather than N — and N=3 is
  already past Dell's documented 3-failures-in-60 s default. **The
  breaker bounds damage; it does not prevent lockout.** Only the
  pre-flight prevents.
- **`Connection: close` may be miscited.** sushy's comment concerns
  long-running persistent connections; a 25-request burst may not be that
  case, and forcing ~12,500 TLS handshakes per run is a real cost.
- **Supermicro is out of scope and untested**, and its Redfish may be
  licence-gated behind `SFT-OOB-LIC` with an undocumented free/licensed
  boundary. Recorded because the expected symptom — a server ingesting
  with a name and serial and almost nothing else — reads as a collector
  bug. Check licensing first. Lenovo XCC and OpenBMC are equally
  untested; they map to `standalone` and nothing more is claimed.
- **HPE iLO 4 is excluded on conformance grounds**, not vendor grounds:
  `MacAddress` rather than `MACAddress`, `Power` rather than
  `PowerState`, the pre-Redfish dotted `@odata.type`, and no standard
  `Storage` at all. Any equally divergent BMC of any brand fails the same
  gate.

## Settled review decisions

The four remaining questions this ADR was reviewed against, and what was
decided.

**The credential breaker trips at three distinct BMCs, and says so
loudly.** Three *different* hosts rejecting the same credential disables
it for the rest of the run: remaining hosts on that credential are
skipped without a connection attempt, hosts on other credentials
continue, and one aggregate error names the credential and the hosts that
rejected it. Three attempts against a *single* BMC cannot arise — a 401
is never retried, so one host produces at most one authentication failure
per run.

**Stated plainly because it is easy to over-claim: this bounds damage, it
does not prevent lockout.** With 16 hosts contacted concurrently, ~16
logins are already in flight before the first rejection returns, so the
effective threshold is concurrency rather than three — and Dell blocks
the source IP at three failures, which is reached before the breaker can
act. The pre-flight against a single host is the mechanism that
prevents; the breaker is the backstop behind it.

**Exit 3 keeps its meaning, and nothing alerts on Job status.** A partial
run must never report success — that is how a bad credential stays
invisible for weeks. But with PARTIAL as the *normal* outcome for
hundreds of independent BMCs, the Job is routinely red, and an alert
nobody can act on gets muted before the day it matters. Alerting is on
staleness instead. `backoffLimit` is **1**, not 0: a ConfigMap mount can
lag at pod start and read as "inventory absent", and with no retry that
loses an entire collection cycle. The retry-sprays-credentials risk that
argued for 0 is already covered by the pre-flight and the breaker.

**`Accept-Encoding: identity` is not sent.** It was proposed as a
one-line decompression-bomb defence, but some BMCs reject the header
outright — turning a theoretical attack into a real, silent zero-data
failure on those hosts, with nothing pointing at the cause. A streaming
byte cap gives the same protection with no compatibility risk.

## Consequences

**A machine no aggregator owns becomes collectable**, including machines
from vendors this platform already has a manager-based collector for.
This does **not** restore a UCS Manager entry point: `--manager-type
UCS_MANAGER` remains deleted (ADR-0014), and reaching a Cisco CIMC over
Redfish is a different protocol to a different endpoint that knows
nothing about domains or service profiles.

**The inventory file is a production-critical, review-gated artefact.**
With `^ocp` correctly not applied, it is the *only* collection filter —
there is no second line of defence. Write access to it is equivalent to
write access to the credential Secret, because it decides where the
credential is sent. It belongs in git and reaches the cluster through
GitOps.

**Supported scale is ~400–1000 hosts per CronJob, extended by sharding
the inventory across CronJobs.** This does **not** reach the platform's
10,000-server target in this shape; at that size the per-server cost
model requires a continuously-running worker pool with a persistent
queue, which is a different program rather than a bigger CronJob. The
10,000 figure is about inventory size, most of which arrives through
aggregators — standalone is the long tail.

**Collector-side Prometheus metrics are impossible.** `/metrics` is
served by the API process; a CronJob pod lives minutes and is never
scraped. So "40 hosts have been failing for two weeks" — a failure of
*absence* — can only be answered by staleness gauges derived from
MongoDB's `last_seen_at`, exported by the API. Until that lands,
staleness detection is a documented manual query, and the ADR says so
rather than implying coverage that does not exist.

**PARTIAL is the normal outcome.** Exit 3 will be common and must not be
paged on; alerting moves to staleness. A run that authenticated nowhere
exits 1, not 3.

**Nothing tombstones a server.** A host removed from the inventory keeps
its document with a frozen `last_seen_at`, and no delete path exists
anywhere in this codebase. Deleting an inventory line makes a server
invisible-but-present.

**A machine listed here *and* registered with an aggregator is collected
by both.** Correctness converges — they share `(vendor, serial)` and
therefore one document — but `manager_id` and `source_provider` are
single-valued and flip each cycle. Documented, not detected.
