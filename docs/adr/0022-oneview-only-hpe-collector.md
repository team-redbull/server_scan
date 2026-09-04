# ADR-0022: HPE servers are collected from OneView only, whatever the iLO generation

Date: 2026-09-04
Status: Accepted — **never validated against live hardware** (see "Status
against real hardware" below, and read it before trusting any number this
collector reports).

Supersedes nothing. Sits beside `0020-dell-identity-from-ome-hardware-from-redfish.md`,
whose split design this deliberately does **not** copy, and beside
`0017-intersight-collector.md`, whose "built entirely from the contract"
status this shares.

## Context

The estate runs a mixed HPE fleet — iLO 4, iLO 5 and iLO 6 in the same
racks. Two collector shapes were available:

1. **The Dell shape (ADR-0020):** the aggregator says who exists and each
   server's own BMC says what it is. Applied here that would mean
   OneView for identity plus a Redfish pass against every iLO.
2. **OneView alone:** three bulk calls per appliance and nothing else.

The Dell shape reads better on paper. It is what ADR-0020 chose for Dell,
for reasons that hold here too: only the aggregator knows the operator's
name, only the BMC reports measured values, and Redfish reports things
OneView does not (thread counts, link state, PSUs, per-DIMM detail).

**It also does not work uniformly across this fleet.** iLO 4 predates
Redfish's useful coverage, so a split design would have a per-generation
branch in the collection path: Gen9-and-older down one road, Gen10+ down
another, with two different sets of field provenance for one vendor's
servers in one inventory. That is exactly the shape that makes a fleet
hard to reason about — "why does this server have thread counts and that
one doesn't" becomes a question about which branch ran, not about the
hardware.

## Decision

**One collection standard for all HP hardware: OneView, for every server,
whatever its iLO generation.**

This is the user's decision, made explicitly for the mixed-generation
fleet, and it is not up for re-litigation in code. Concretely:

- No Redfish. The collector does not build `RedfishTarget`s, does not
  read BMC credentials, and there is no `INVENTORY_ONEVIEW_BMC_*`.
- No iLO-generation branching in the collection path. Every server goes
  through the identical code. `mpModel` is read and *reported* (it is how
  the run explains an unreadable subresource) but never branched on.
- What OneView cannot report is reported as `None` — "not read this run"
  — which `IngestService` carries forward, never as zero.

### What this costs on Gen10/Gen11

Less than the first draft of this ADR assumed. `GET /rest/server-hardware`
returns the **complete** `ServerHardwareV12` object per member, not a
summary — verified by diffing the collection's field table against the
single-server GET — and `expand=all` folds every subresource's data into
that same response. So the whole appliance is a handful of requests, and
the only genuinely per-server endpoints are `/powerSupplies`,
`/processors`, `/thermal`, `/networkAdapters` and `/utilization`.

Against what a Redfish pass would have added:

| Field | OneView | A Redfish pass |
|---|---|---|
| `cpu_threads` | nowhere on `server-hardware`; only `/processors`, one call per server | `Processor.TotalThreads` |
| `ProviderNic.speed_mbps` | absent from `portMap` | `EthernetInterface.SpeedMbps` |
| `ProviderNic.link_state` | absent from `portMap` | `EthernetInterface.LinkStatus` |
| PSUs | `/powerSupplies`, one call per server — **and richer than Redfish's**, see below | `Power.PowerSupplies` |
| Per-DIMM detail | free with `expand=all`, but unused — `ProviderServer` has no field for it | same |
| GPU VRAM | **no memory field anywhere**; VRAM comes from the GPU catalog | `Processor` + `EnvironmentMetrics` |
| Component temperature | **does not exist** — `/thermal` is fans only, in percent | `Thermal.Temperatures[]` |
| Drives | free with `expand=all`, in two different schemas | one schema |

Every missing field is `None`, so nothing is *wrong*, only absent, and
adding a Redfish pass for iLO 5+ later would be additive.

### Cost model, and the one per-server call this collector makes

Per sweep, with `N` servers on the appliance:

| Tier | Calls | Yields |
|---|---|---|
| Bulk | `1` login + `⌈profiles/256⌉` + `⌈N/25⌉` + `1` logout | names, serials, models, UUIDs, memory, CPU counts, MACs, iLO addresses, health — **and, via `expand=all`, DIMMs, drives, GPUs and PCI devices** |
| + PSUs | `+ N` | `ProviderServer.psus` |

**PSUs are collected, on by default, and that is a deliberate exception
to the cheapness above.** `ProviderServer.psus` was added on 2026-09-01
and **no provider in this repo has ever populated it** — `IngestService`
hardcoded `Power(psus=[])`, so the health engine's `power.psu_count` and
`power.failed_psu_count` metrics have had nothing to read since they were
written. A server with a dead PSU reported HEALTHY on power exactly like
one with two good ones. OneView closes that, and closes it better than
either Cisco collector could: `PowerCapacityWatts` is documented in
Watts, and `Oem.Hpe.PowerSupplyStatus.State` distinguishes `Failed` from
`Degraded` from `ACPowerLost` from `OverTemperature`, which maps to real
health rather than a boolean.

`INVENTORY_ONEVIEW_COLLECT_PSUS=false` turns it off, and
`INVENTORY_ONEVIEW_PSU_CONCURRENCY` (default 8) bounds the fan-out. Off
means every server's `psus` is `None` — unread, carried forward — never
an empty list.

The undocumented part is handled rather than assumed: `/powerSupplies`
returns a `SubResourceV10` envelope but has **no matching
`SubResourceName` enum value**, so whether `expand=all` already includes
it is undetermined in HPE's own docs. The collector looks in the expanded
payload first and only calls per server for what is missing, and logs
`oneview.power_supply_source` with both counts — because the difference
between the two routes is ~15 requests and ~2500, and an undocumented gap
must not turn one into the other silently.

### What is deliberately not collected

- **Fleet firmware** (`GET /rest/server-hardware/*/firmware`, one bulk
  call). `ProviderServer` has no system-firmware field — only
  `StorageDrive.firmware_version` and `Gpu.firmware_version`, both of
  which come from the expanded payload already. Collecting it would cost
  a call and produce nothing anything downstream can store.
- **Fans** (`/thermal`). One call per server for a field this platform
  does not model, and it is fan *speed in percent* — the documented
  schema has five properties and no RPM.
- **Temperature and power draw** (`/utilization`). This is the one worth
  being explicit about, because it looks like the obvious gap. OneView's
  only temperature is `AmbientTemperature`, documented as "**inlet air
  temperature** in degrees Celsius during this sample interval" — it is a
  *different physical quantity* from the per-component temperatures the
  Cisco collectors report into `Gpu.temperature_celsius`, and it is
  *historical sampled data*, not a current reading (`refresh=true`
  explicitly "will not include any refreshed data"). Putting an inlet
  sample into a field that means "this component's temperature" would
  make two vendors' numbers look comparable when they measure different
  things. Reporting nothing is the honest answer; if inlet temperature is
  wanted later it needs its own field, not this one.
- **`/processors`** for `cpu_threads`. One call per server for a single
  integer, when `None` already means "not read" and ingest carries the
  stored value forward. **`2 x cores` is explicitly not used** — that is
  the heuristic ADR-0020 deleted from the Dell collector, and it must not
  reappear. The probe does call `/processors` on a sample, but as a
  *cross-check* on the core-count mapping, not as a collection path.

### The traps, and how each is handled

Each is documented with its HPE source in `docs/hpe-collectors.md`; this
is the index and the consequence.

1. **The name comes from the server profile.** `server-hardware.name` is
   documented as "the location based name … formed by concatenating the
   enclosure name and the bay number" for blades and "the serial number
   prefixed by word ILO" for racks; `serverName` is "the name of the
   server **as reported by the iLO**", i.e. an OS hostname via HPE
   Agentless Management Service. Neither carries a site token or matches
   a classification rule. Only the profile's `name` ("Unique display name
   of this Server Profile") does. This is the exact trap ADR-0009 records
   for UCS blades named after their chassis slot.
   **Hardware with no assigned profile is skipped**, counted, and logged
   once per appliance. It has no operator-assigned name at all, so
   ingesting it would create a server that parses to no site and matches
   no rule — and an unassigned server is by definition carrying no
   workload. The alternative, falling back to `ILO<serial>`, buys a
   document nothing downstream can use.
2. **`processorCoreCount` is per processor.** HPE: "Number of cores
   available **per processor**". This platform's `cpu_cores` is
   whole-system (`..redfish.mapping` sums `TotalCores`). So
   `cpu_cores = processorCount * processorCoreCount`. Writing the field
   through unmultiplied would under-report every two-socket server by
   half, silently, forever. Either half missing yields `None`, not a
   partial product.
3. **`memoryMb` is MiB, and HPE says so inline:** "Amount of memory
   installed on this server hardware in MiB (1 MiB = 1,048,576 bytes)".
   No assumption is being made — contrast ADR-0017's `TotalMemory`, which
   carries no documented unit anywhere and is still an open 4.86% risk.
4. **`count=-1` means 64 on `/rest/server-profiles`.** Verbatim:
   "Providing a -1 for the count parameter will restrict the result set
   size to 64 server profiles. The maximum number of profiles is
   restricted to 256". An explicit `count` is always sent, never `-1`,
   and `nextPageUri` is followed to the end.
   **Truncation is detected rather than assumed away.** HPE says "the
   list is truncated" without saying whether `nextPageUri` continues past
   256. So `get_all` compares what it fetched against the collection's
   own `total` and, when they disagree, logs `oneview.collection_truncated`
   at ERROR naming both numbers and saying servers were not collected.
   Whether the cap is per request or per query is the highest-consequence
   open question below, and `tools/verify_oneview.py` prints the answer.
5. **`X-Api-Version` is discovered and clamped.** `GET /rest/version`
   answers `currentVersion`/`minimumVersion` with no `Auth` and no
   version header, so the version is negotiated per appliance at the
   start of a run. It is then clamped to `MAX_TESTED_API_VERSION` (8000,
   OneView 10.20 — the newest reference these mappings were read
   against). The clamp exists because HPE guarantees an API version's
   behaviour "remains the same … upward compatible from release to
   release" for *older* versions, and guarantees nothing about a newer
   one: an appliance upgraded to a version this code has never seen would
   otherwise silently hand us a moved field. The one case the clamp is
   skipped is an appliance whose `minimumVersion` is already above 8000,
   where sending 8000 would simply be rejected; that logs a warning
   naming `tools/verify_oneview.py`.
   `INVENTORY_ONEVIEW_API_VERSION` overrides both, validated against the
   appliance's own range — the difference between "the appliance was
   upgraded" and "the collector broke".
6. **The session is always deleted.** `DELETE /rest/login-sessions` runs
   in `__aexit__`, best-effort and swallowing its own errors so it can
   never mask the failure a caller is already handling. An appliance
   allows 2400 active sessions and **960 from one source IP**, each
   living 24 idle hours; a CronJob that leaks one per run burns that
   budget from one pod address.
7. **`InsufficientFirmware` is "could not read", not zero.** Every
   subresource on an iLO-4 server is documented to fail this way ("The
   minimum version to collect some types of inventory is iLO 5 v1.20").
   Only `collectionState == "Collected"` yields data; every other state
   — including `CollectedStale`, which HPE defines as data that may be
   "out of date **or missing** due to the server state" — maps to `None`.
   A `Storage` collection that reported zero drives once took a machine
   from CRITICAL to HEALTHY and logged that the drive had recovered.
   The skips are logged **once per appliance**, aggregated by state and
   iLO generation (`InsufficientFirmware/iLO4: 112`), not once per host.
   Whether the *top-level* fields also come back empty on iLO 4 is
   undocumented; the mapping treats an absent value as `None` and a
   present one as real, with no assumption either way, and
   `tools/verify_oneview.py` reports the answer as a populated-fields
   table split by generation.
8. **Local storage has two mutually exclusive schemas, and v1 is not a
   legacy path.** `/localStorageV2` (stock Redfish `Storage`) appears
   "starting with Gen 10 Plus … instead of (or in addition to)
   `/localStorage`", which means this estate's **iLO-4/Gen9 half answers
   v1 only**. Both are read, picked by what the server actually offers
   rather than guessed from the model string, and V2 wins where a server
   reports both. Two traps inside it: `CapacityGB` is documented by HPE
   as "the marketing capacity (base 10)" and is never used
   (`CapacityMiB`, or `CapacityLogicalBlocks * BlockSizeBytes`, is), and
   on V2 **NVMe is reported in `Protocol`, not `MediaType`** — whose enum
   has only `HDD` and `SSD`, so reading it alone reports every NVMe drive
   as an SSD. The shared `..redfish.mapping.media_type_of` already
   resolves both, and v1's extra `SMR` value onto HDD.
9. **GPU VRAM comes from the catalog.** OneView reports GPUs as
   `Devices` entries with `DeviceType: "GPU"` and a model string, and
   **no memory field anywhere** — not on the device, not on
   `server-hardware`, not in the `Processor` schema. So `memory_bytes` is
   always `None` from this collector and is filled in downstream from
   `INVENTORY_GPU_MODELS` plus the built-in table (ADR-0021).
   HPE rebrands NVIDIA cards, and its product names did **not** match the
   catalog as it stood: `"HPE NVIDIA L40S 48GB PCIe Accelerator"` shares
   no normalized key with `"L40S"`. Matching was extended rather than
   filled with guessed alias strings — see below.

### GPU matching, extended for rebranded product names

`gpu_catalog._normalize` now also drops a leading `HPE`/`HP` and a
trailing marketing noun (`Accelerator`, `Kit`, `Adapter`, …). Lookup then
tries, in order: the normalized string; the same string minus a trailing
bus word (`PCIe`, `SXM4`); and finally a `<model> <N>GB` spelling against
a row keyed on the bare model — **accepted only when N GB equals that
row's own VRAM**.

That last rule is the important one. It is the only inexact match in the
catalog, and it is self-validating: `"HPE NVIDIA A16 64GB PCIe
Accelerator"` matches *nothing*, because this table models the A16 as the
four 16GB GPUs it carries and 64 ≠ 16. A wrong capacity produces an
honest miss, never a wrong number. That is why this was done as a
matching rule rather than as a dozen guessed alias strings: aliases would
have had to be invented for spellings nobody here has seen, and an
invented alias that is subtly wrong reports a confident wrong VRAM.

Splitting on words rather than on the joined key matters too:
`"T4 16GB"` joins to `T416GB`, which a regex over the joined string reads
as `T` + `416GB`.

## One appliance — and the 2500-server ceiling, documented not coded

`INVENTORY_ONEVIEW_IP` is a **single endpoint**, exactly like
`INVENTORY_OME_IP`, resolved through `EnvConnectionResolver` with no
parsing layer of its own. That keeps ADR-0012's "one endpoint and one
login per `ManagerType`" invariant intact for this vendor. One appliance
failing is the run failing; there is no partial-success accounting.

**The known limit, recorded here so the next person finds it rather than
rediscovering it from a truncated fleet.** HPE's OneView 10.0 Support
Matrix (docId `sd00006056en_us`, "Configuration maximums"):

> "The total number of servers in an HPE OneView appliance cannot exceed
> **2500 servers**."

with the deployment footnote:

> "The total number of servers in an HPE OneView **VM appliance cannot
> exceed 2500 servers if the VM OVA is deployed using a VMware vSphere
> ESXi hypervisor**. … **For hypervisors other than ESXi, the HPE OneView
> appliance can manage and monitor up to a maximum of 1024 servers.**"

Also: 2500 assigned server profiles, and only **100 unassigned** ones.

This estate is one appliance, comfortably inside that, and the user has
confirmed that is not going to change. **An estate that outgrows one
appliance needs a second endpoint** — either `INVENTORY_ONEVIEW_IP`
becomes a list or each appliance gets its own CronJob — and that work is
deliberately not done now. `docs/notes/oneview-api.md` originally
recommended building multi-endpoint from day one; that recommendation
predates this decision and has been corrected in place so the research
file does not contradict the shipped design.

The symptom to watch for, if this is ever wrong, is not a crash: it is
`oneview.collection_truncated` at ERROR, or a fleet that quietly stops
growing at 2500.

## Decision 2: hand-rolled on `httpx`, not the `hpeOneView` SDK

`hpeOneView` 11.4.0 is maintained and small, so the Intersight reason
(a 57.6 MB wheel of 10,112 generated modules) does not apply. It is
rejected for a different one: the OneView protocol is *trivially* small —
one POST for a token, one header, paginated GETs — while the SDK is
synchronous (`http.client` directly, so every call would need
`asyncio.to_thread`), depends on `future`, and pins `docutils<0.18`, which
constrains the whole air-gapped wheel mirror. Three functions of `httpx`
replace it.

Four behaviours were still taken from reading the SDK's source, because
they are the ones a hand-rolled client learns the hard way:
`loginMsgAck` force-set on every login; the default version being
`currentVersion`; the version validated against `[minimumVersion,
currentVersion]`; and the page loop's guard against a `nextPageUri` equal
to the page's own `uri`, which exists because appliances have returned
one and without it the loop never terminates.

**Not** taken from the SDK: its TLS posture. It trusts any certificate
unless a bundle is passed. `INVENTORY_ONEVIEW_VERIFY_TLS` defaults to off
for the same stated reason as `INVENTORY_OME_BMC_VERIFY_TLS` — an
appliance in an air-gapped estate ships a self-signed certificate — and
turning it on with a real chain is the documented scalable answer, not a
permanent exemption.

## Status against real hardware

**Nothing in this collector has ever run against a live OneView
appliance, and there is no way to change that from this repository.**

Worth stating first, because it is the premise the whole OneView-only
decision rests on: **the appliance really does manage the iLO-4 range.**
OneView 10.0's Support Matrix still lists Gen8 and Gen9 as managed rack
servers — Gen8 under its era's names, `DL360p`/`DL380p`/`DL385p` — so
every server in this mixed fleet is in scope for the appliance, whatever
its subresources turn out to report.

- **There is no OneView equivalent of Cisco's UCS Platform Emulator.**
  The 60-day OneView trial is a *real appliance*, not a hardware
  simulator; with no HPE hardware attached, `GET /rest/server-hardware`
  returns an empty collection. It would validate authentication,
  versioning, pagination and error handling — more than Intersight ever
  got — and **zero** field mappings, which is where every defect UCSPE
  found actually lived.
- HPE Synergy Data Center Simulator (DCS) is described in community posts
  as partner-only and is Synergy (blades), not the DL rack servers this
  estate runs. Unconfirmed either way.

So this collector sits between UCS (a real emulator, five defects found)
and Intersight (nothing at all). **`uv run python -m tools.verify_oneview`
is the outstanding action**, and it is a read-only probe: it writes
nothing, makes no MongoDB connection, and logs out. Run it against the
real appliance and record what it prints here.

### What only a live appliance can settle

Ordered by how much damage a wrong guess does.

0. **Does `processorCount * processorCoreCount` equal the real core
   count?** This is the probe's *headline* line, and the cheapest
   possible guard against the trap above: `/processors` reports each
   socket's own `TotalCores`, so summing them measures the same quantity
   the bulk mapping computes. Agreement confirms the mapping; a
   disagreement means every server's core count is wrong fleet-wide,
   silently. One sampled server settles it.
1. **Is the 256 cap on `/rest/server-profiles` per request or per
   query?** If per query, the whole "profile supplies the name" design
   cannot enumerate an estate with more than 256 profiles and must shard
   by `filter`. The client detects and logs this rather than hiding it,
   but detection is not a fix. *Probe section 2.*
2. **Do the top-level hardware fields (`memoryMb`, `processorCount`,
   `processorCoreCount`, `portMap`) populate on an iLO-4 server?** If
   they do not, iLO-4 machines get identity and nothing else, and the
   cost of the OneView-only decision is much higher than the table above
   suggests. *Probe section 4, as a populated-fields table by generation.*
3. **What does `mpModel` contain per generation?** One documented example
   (`iLO4`), no enum, nothing about iLO 7. The mapping parses the
   trailing integer and reports a non-match as unknown, so a surprise
   value degrades to "unknown" rather than to a wrong branch — but the
   real value set is worth knowing. *Probe section 3.*
4. **Which `mpIpAddresses` entry is reachable, and is there always one?**
   Ordering and cardinality are both undocumented. The mapping discards
   link-local/SLAAC and prefers `Static` → `DHCP` → `Lookup`, falling
   back to `mpHostName`. A wrong pick means a stored BMC address nothing
   can reach. *Probe section 5, which prints `mpHostInfo` verbatim.*
5. **What happens when `X-Api-Version` is omitted?** Documented as
   required, with the omission case unstated. *Probe section 1, which
   issues the same request with and without the header.*
6. **Does `serverName` contain anything without HPE AMS?** Only matters
   if a name fallback for profile-less servers is ever wanted. *Probe
   section 6.*
7. **Do GPUs also appear under `/processors` as `ProcessorType: "GPU"`?**
   If so, a future Redfish pass would get GPUs for free on iLO 5+.
   *Probe section 7.*
8. **Do HPE's real GPU product names match the catalog?** The matching
   above was built against realistic spellings, not observed ones. The
   probe prints every GPU string the estate reports with a CATALOG
   HIT/MISS verdict. *Probe section 7.*
9. **Is `subResources` an object or an array?** Documented per-field but
   not as a container. Both are accepted; the probe says which is real so
   the dead branch can be deleted. *Probe section 9.*

## Consequences

- `--manager-type ONEVIEW` works; `tools/run_collector.py`'s
  `_PROVIDER_FACTORIES` has an entry and `OPENMANAGE`'s "not implemented"
  neighbours are down to none.
- A Helm CronJob ships disabled, on a 4-hour cadence — between Cisco's
  hourly and the BMC-touching collectors' 6-hourly, because this is three
  bulk calls per appliance rather than thousands of requests against
  embedded hardware.
- `INVENTORY_ONEVIEW_IP` is one endpoint like every other vendor's, so
  nothing about `EnvConnectionResolver`, the collector Secret or the
  `Manager` projection is special-cased for HPE.
- This is the first collector to populate `ProviderServer.psus`, which
  means the health engine's `power.*` metrics start producing verdicts
  for HPE servers where they produced nothing for anyone before. An
  estate with a long-dead PSU will see health states change on the first
  sweep — that is the feature working, not a regression.
- The GPU catalog's matching rules changed for every vendor, not only
  HPE. The change only ever widens a match to another spelling of the
  same card, and the capacity check makes the one inexact rule
  self-validating, but it is a shared-code change and is called out here
  rather than buried in the HPE work.
