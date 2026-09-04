# HPE collectors — verified implementation facts

This is the technical reference behind
`app.infrastructure.providers.oneview` (`client.py`, `mapping.py`,
`provider.py`). Every docstring in those modules points here rather than
carrying its own explanation, so the `##` headings below are load-bearing:
renaming one breaks the cross-references in the source files.

It is for whoever is about to change the HPE collector.

**Provenance, stated up front.** Everything here comes from HPE's own API
Reference — `dp00003271en_us` (OneView for VMs, 8.00 / API 4600) and
`dp00006616en_us` (OneView for HPE Synergy, 10.20 / API 8000) — plus the
`hpeOneView` 11.4.0 SDK source and the OneView 10.0 Support Matrix
(`sd00006056en_us`). The full research notes with reproduction
instructions for every citation are `docs/notes/oneview-api.md`.

**None of it has been confirmed against live HPE hardware.** There is no
OneView equivalent of Cisco's UCS Platform Emulator: the 60-day trial is a
real appliance, not a simulator, so with no HPE hardware attached it
enumerates nothing and validates zero field mappings. That puts this
collector in the same state ADR-0017 records for Intersight. Treat every
line below as a documented contract, not as an observed fact, and run
`uv run python -m tools.verify_oneview` before trusting a number.
See `docs/adr/0022-oneview-only-hpe-collector.md`.

## OneView is the only source, whatever the iLO

There is no Redfish pass, no BMC login and no per-generation branch. An
iLO 4 server and a Gen11 go through the identical code. This is a
deliberate decision for a mixed iLO 4/5/6 fleet — one collection standard
for all HP hardware — and what it costs on the newer half (thread counts,
NIC speed and link state, PSUs, GPU telemetry) is tabulated in ADR-0022.

Do not "fix" this by adding a Redfish hand-off. If a future change adds
one, it must be additive for iLO 5+ and must not change what OneView
already fills.

## REST surface and cost model

One appliance answers at `https://<appliance>/rest/...`.

**`GET /rest/server-hardware` returns the complete `ServerHardwareV12`
object per member, not a summary** — verified by diffing the collection's
`members[]` field table against `GET /rest/server-hardware/{id}`'s own
response table: every field in the collection member exists in the
single-resource GET, with none present in only one. Adding `expand=all`
folds each server's subresource *data* into that same response. So the
whole appliance is a handful of requests:

- `GET /rest/version` — `{"currentVersion", "minimumVersion"}`. The one
  operation documented as requiring neither `Auth` nor `X-Api-Version`,
  which is what lets `health_check` prove reachability without spending
  a session.
- `POST /rest/login-sessions` → `{"sessionID"}`; `DELETE` the same path
  logs out, 204.
- `GET /rest/server-profiles` — names, and the template each came from.
- `GET /rest/server-profile-templates` — template URI → template name.
- `GET /rest/server-hardware?expand=all` — serials, models, addresses,
  CPU/memory, `portMap`, **and** every subresource's data (DIMMs, drives
  in both schemas, PCI devices including GPUs) inline.
- `GET /rest/server-hardware/{id}/powerSupplies` — the one per-server
  call, gated by `INVENTORY_ONEVIEW_COLLECT_PSUS`.

With `N` servers on the appliance:

| Tier | Calls | Yields |
|---|---|---|
| Bulk | `1` + `⌈profiles/256⌉` + `⌈N/25⌉` + `1` | everything except PSUs |
| + PSUs | `+ N` | `ProviderServer.psus` |

That bulk tier is two orders of magnitude cheaper than ADR-0020's Dell
design, which costs ~25 round trips *per server*.

**Not collected, deliberately** — each would cost calls for something
`ProviderServer` cannot carry, and the reasoning is in ADR-0022:
fleet firmware (`GET /rest/server-hardware/*/firmware`, one bulk call —
but there is no system-firmware field), fans (`/thermal`),
temperature and power draw (`/utilization`), and `/processors` for
`cpu_threads`.

## Sessions

`POST /rest/login-sessions` takes `{"userName", "password",
"loginMsgAck"}`. `Content-Type: application/json` is required; any other
value returns 415, and absent it `application/octet-stream` is assumed.

`loginMsgAck` is always sent as `true`, copying the SDK
(`connection.py:468`): an appliance configured to require
login-message acknowledgement rejects a login without it, and there is no
downside on one that does not.

The token comes back as `sessionID` and is replayed in a bare **`Auth`**
header — *not* `Authorization: Bearer`. The SDK sends it lowercase
(`auth`); HTTP header names are case-insensitive, and this collector uses
the documented spelling.

**Always log out.** The limits are real and enforced:

- 2400 active sessions per appliance (`SESSION_CRITICAL_LIMIT`).
- **960 from one source IP** (`SESSION_CLIENT_LIMIT`) — a collector pod is
  one source IP.
- 24-hour default idle timeout.

So `OneViewClient.logout()` runs from `__aexit__`, is best-effort, and
swallows its own errors so it can never mask the failure a caller is
already handling.

## API version

`X-Api-Version` is `required` on every documented operation. Which
versions an appliance accepts is a runtime question with an
unauthenticated answer, so the collector discovers it rather than
hardcoding a constant.

**And then clamps it** to `client.MAX_TESTED_API_VERSION` (8000, OneView
10.20 — the newest reference these mappings were read against). The
asymmetry is the point: HPE states an API version's behaviour "remains
the same … upward compatible from release to release", so an *older*
version stays correct on a newer appliance, while a *newer*
`currentVersion` is a contract nobody here has read. Without the clamp,
upgrading an appliance silently changes what the collector is handed.

The clamp is skipped in exactly one case — an appliance whose
`minimumVersion` is already above 8000, where sending 8000 would be
rejected outright. That logs `oneview.api_version_above_tested` at WARNING
naming `tools/verify_oneview.py`.

`INVENTORY_ONEVIEW_API_VERSION` overrides both, and is validated against
the appliance's own `[minimumVersion, currentVersion]` range before use.

Version→release, for reading the table above: 8.50/5600, 9.00/6600,
10.00/7600, 10.20/8000, 11.40/8800. HPE supports an API version for two
years after its release.

**UNVERIFIED:** what an appliance does when the header is omitted
entirely. Documented as required; the omission case is not documented.
`tools/verify_oneview.py` section 1 settles it by issuing the same
request twice.

## Pagination, and the 256 ceiling

Every collection GET returns `start`, `count`, `total`, `members`,
`nextPageUri`, `prevPageUri`. Following `nextPageUri` until it is null is
the **only** correct loop: HPE states the appliance "may limit the number
of resources returned", so `start += count` can skip members.

Two guards, both copied from the SDK (`resource.py:778`) and both real:
a `nextPageUri` equal to the page's own `uri`, and a repeat of a URI
already fetched. Appliances have returned the former; without the guard
the loop never terminates.

### Never send `count=-1` to `/rest/server-profiles`

Verbatim, and identical in both API references:

> "Providing a -1 for the count parameter will restrict the result set
> size to 64 server profiles. The maximum number of profiles is
> restricted to 256, i.e., if user requests more than 256, this will be
> internally limited to 256."

and, in the example prose: "If the number of profiles does not exceed the
limit, then all profiles are returned; otherwise, **the list is
truncated**."

This is the single most likely way to ship a collector that silently sees
a fraction of the estate. An explicit `count` is always sent
(`INVENTORY_ONEVIEW_PAGE_SIZE`, default 256, which is also safe for
`/rest/server-hardware`, whose maximum is documented only by absence).

### Truncation is detected, not assumed away

"The list is truncated" does not say whether `nextPageUri` is populated
past the 256th profile. Rather than guess, `get_all` compares what it
fetched against the collection's own `total` and, when fewer members were
fetched, logs `oneview.collection_truncated` at **ERROR** naming both
numbers and stating that servers were not collected.

A collector that silently sees a third of the estate looks exactly like a
healthy run against a smaller fleet — which is how a wrong number stays
invisible for weeks.

**UNVERIFIED and the highest-consequence open question:** whether the cap
is per *request* (paging works) or per *query* (profile 257 is
unreachable and the design needs `filter` sharding). The configuration
maximum is 2500 assigned profiles, so this is not hypothetical.

## The name trap

`ProviderServer.name` comes from the **server profile**. This is the same
trap ADR-0009 records for UCS, in the same shape.

| Field | HPE's own description |
|---|---|
| `server-hardware.name` | "For blade servers, it is the location based name of the server, which is formed by concatenating the enclosure name and the bay number. For rack servers, it is the serial number prefixed by word "ILO" (e.g. ILOUSE31835LS)." |
| `server-hardware.serverName` | "The name of the server as reported by the iLO. The iLO gets this information from a running operating system that has monitoring software installed, like Agentless Management Service." |
| `server-profiles[].name` | "Unique display name of this Server Profile." |

The first is a bay location and carries neither a site token nor a
classifiable pattern. The second is an OS hostname that only exists where
HPE AMS is running — a decoy; what it contains without AMS is
**UNVERIFIED**. Only the third is the operator's name.

**Hardware with no assigned profile is skipped**, counted, and logged
once per appliance as `oneview.hardware_without_profile`. Such a server
has no usable name at all: falling back to `ILO<serial>` would create a
document that parses to no site and matches no classification rule, and
an unassigned server is by definition carrying no workload.

### The join, and the serial

Hardware → profile is `server-hardware.serverProfileUri` ("If not
assigned this value is null"); profile → hardware is
`server-profiles[].serverHardwareUri`. The collector joins on the former.

`associatedServer` is a trap and is not used: it is a *serial*, not a
URI, and it is sticky — "the server hardware that the server profile is
currently applied to **or was most recently assigned to**".

`ProviderServer.serial` is `server-hardware.serialNumber`, the physical
one. **Never the profile's** `serialNumber`, which is documented as
possibly "a virtual serial number, user defined serial number or physical
serial number", with `serialNumberType` defaulting to `Virtual`. Ingest
correlates on `(vendor, serial_normalized)`, so a virtual serial would
split one machine into two documents.

## CPU, memory and the units

| This platform | OneView | Note |
|---|---|---|
| `cpu_sockets` | `processorCount` | "Number of processors installed" |
| `cpu_cores` | `processorCount * processorCoreCount` | **see below** |
| `cpu_threads` | — | not reported anywhere; `None` |
| `cpu_model` | `processorType` | |
| `memory_total_bytes` | `memoryMb * 1048576` | |

**`processorCoreCount` is per processor.** HPE: "Number of cores
available **per processor**". This platform's `cpu_cores` is a
whole-system figure (`..redfish.mapping` sums every processor's
`TotalCores`). Writing `cpu_cores = processorCoreCount` would under-report
every two-socket server by half, silently, forever. Either half absent
yields `None`, never a partial product.

**`memoryMb` is the best-documented unit of any vendor in this repo:**
"Amount of memory installed on this server hardware in MiB (1 MiB =
1,048,576 bytes)". The factor is spelled out inline. Contrast ADR-0017's
`TotalMemory`, which carries no documented unit anywhere.

**No thread count exists** on `server-hardware` — there is no
`logicalProcessorCount`, `threadCount` or hyperthreading flag anywhere in
`ServerHardwareV12`. The only source is `/processors`' `TotalThreads`,
one call per server, which this collector does not make. `None` is
reported and ingest carries the stored value forward. **`2 x cores` is
the heuristic ADR-0020 deleted from the Dell collector and must not
reappear here.**

**`/processors` is the cross-check on the multiplication, and it is the
probe's headline.** Each socket reports its own `TotalCores`, so
`sum(TotalCores)` measures the same quantity `processorCount *
processorCoreCount` computes. If they disagree, the core count is wrong
for the whole fleet, silently — `tools/verify_oneview.py` prints the
verdict twice, once at the top and once in its closing summary.

## Subresources and `collectionState`

`GET /rest/server-hardware` returns subresource *metadata* with an empty
`data` field; `expand=all` populates it. That is the lever this collector
uses — one paginated pass instead of N per-server calls — paged at 25
(`client.EXPANDED_PAGE_SIZE`, deliberately a constant rather than a
setting, since response size is HPE's own reason for `expand` being off
by default).

Documented `SubResourceName` values: `AdvancedMemoryProtection`,
`Devices`, `LocalStorage`, `LocalStorageV2`, `MPSettings`, `Memory`,
`MemoryList`, `Unknown`. The collector reads `Devices`,
`LocalStorageV2` and `LocalStorage`. `Memory` is not read: this
platform's `ProviderServer` has no per-DIMM field, and `memoryMb` already
gives the total.

**Only `Collected` yields data.** Every other state maps to `None` —
"could not read this run" — never to zero or an empty list.

| State | HPE's meaning | Mapped to |
|---|---|---|
| `Collected` | "successfully collected … current at the time of collection" | the data |
| `CollectedStale` | "may be out of date **or missing** due to the server state … typically when the server is powered off or in POST" | `None` |
| `CollectionError` | "An error occurred during the collection" | `None` |
| `InsufficientFirmware` | "The iLO firmware on the server is too low … The minimum version to collect some types of inventory is iLO 5 v1.20." | `None` |
| `NotCollected` | initial state before any collection | `None` |
| `Unknown` | "Unable to determine … or null returned" | `None` |

`CollectedStale` is excluded deliberately, and it is the least obvious
row: "successfully collected" reads like usable data until you reach "or
missing". A powered-off server reporting zero drives is exactly the bug
the `None` contract exists to prevent — a `Storage` collection that
returned zeros once took a machine from CRITICAL to HEALTHY and logged
that the drive had recovered.

**Every subresource on an iLO-4 server returns `InsufficientFirmware`.**
The skips are logged once per appliance, aggregated by state and
generation (`oneview.subresources_unreadable`, e.g.
`{"InsufficientFirmware/iLO4": 112}`), not once per host: on a mixed
estate a per-host line would bury the run's real output.

**UNVERIFIED:** whether the *top-level* fields (`memoryMb`,
`processorCount`, `portMap`) also come back empty on iLO 4. The docs say
nothing either way, so the mapping treats an absent value as `None` and a
present one as real, with no assumption in either direction.
`tools/verify_oneview.py` section 4 prints a populated-fields table split
by generation, which is the answer.

**UNVERIFIED:** whether `subResources` is a JSON object keyed by name or
an array of envelopes. HPE documents the fields, not the container. Both
shapes are accepted; probe section 9 says which is real.

## Storage — two schemas, one of them dangerous

> "Starting with Gen 10 Plus, certain storage adapters will provide
> `/localStorageV2` instead of (or in addition to) `/localStorage`."

So both are read, and **V2 wins where a server reports both**.

**v1 is not a legacy path you can skip.** The split is at Gen10 Plus,
which means this estate's iLO-4/Gen9 half answers `/localStorage` only.
Both are read, and the schema is chosen by what the server actually
offers rather than guessed from its model string.

`LocalStorageV2` is stock Redfish `Storage`: `Drives[]` with
`CapacityBytes` documented in bytes, `MediaType`, `Protocol`,
`RotationSpeedRPM`, `FailurePredicted`,
`PredictedMediaLifeLeftPercent`, `Status`.

**One trap in it: `MediaType`'s enum is only `HDD` and `SSD` — NVMe
lives in `Protocol`.** Reading `MediaType` alone reports every NVMe drive
in the fleet as an SSD. This is the same class of mistake ADR-0020 had to
resolve on Dell by inferring media from three fields at once; here the
shared `..redfish.mapping.media_type_of` already checks `Protocol` first,
which is exactly why it is reused rather than reimplemented.

`LocalStorage` is HPE's SmartStorage schema with **three overlapping
capacity fields**:

| Field | HPE's description |
|---|---|
| `CapacityGB` | "Total capacity of the drive in GB. **This denotes the marketing capacity (base 10)**" |
| `CapacityMiB` | "Total capacity of the drive in MiB" |
| `CapacityLogicalBlocks` × `BlockSizeBytes` | exact |

The collector uses `CapacityMiB * 1048576`, falling back to
`CapacityLogicalBlocks * BlockSizeBytes`. **Never `CapacityGB`** — HPE
says outright it is the marketing number.

V1's `MediaType` also carries `SMR` (shingled magnetic recording), one
value the Redfish enum has no member for. It is a hard disk, and the
shared `..redfish.mapping.media_type_of` already maps it onto HDD rather
than losing it to UNKNOWN.

## NICs — `portMap`

`portMap.deviceSlots[].physicalPorts[]` supplies `nic_macs` and
`ProviderServer.nics`.

**`virtualPorts` are deliberately not included.** They are the
FlexNIC/partition MACs carved out of a physical port — the same
PHYSICAL-vs-VNIC distinction `ProviderAttachment.interface_kind` models
for UCS. Feeding both levels into `nic_macs` flat would inflate a set
ingest correlates identity on.

**No link speed and no link state exist anywhere in `portMap`.** There is
no `speedMbps` and no up/down field. `ProviderNic.speed_mbps` is `None`
and `link_state` is `"UNKNOWN"`; neither is synthesised.

`interconnectUri` / `interconnectPort` would be the natural
`ProviderAttachment` source for blades in an enclosure, mirroring UCS
fabric paths. Out of scope, noted so it is not rediscovered.

## The management-processor address

`mpHostInfo.mpIpAddresses[]` is a **list** mixing IPv4 and IPv6, each
entry carrying a `type`: `DHCP`, `Static`, `SLAAC`, `LinkLocal`,
`LinkLocal_Required`, `Lookup`, `Undefined`.

The collector discards `LinkLocal`, `LinkLocal_Required` and `SLAAC`
outright — an IPv6 link-local address is unroutable without a zone index,
which nothing downstream carries — then prefers `Static` → `DHCP` →
`Lookup` → `Undefined`, and falls back to `mpHostName`. The result is
stored as `https://<address>`.

**UNVERIFIED:** neither the ordering nor the cardinality of
`mpIpAddresses` is documented, and there is no statement that an entry is
always present. This preference is a stated assumption. A wrong pick
means a stored BMC address nothing can reach — exactly the risk ADR-0020
carries for iDRAC addresses. Probe section 5 prints `mpHostInfo` verbatim.

## iLO identity

`mpModel` — "The model type of the iLO, **such as `iLO4`**." That is the
only documented example. There is no enum, no pattern, and nothing at all
about iLO 5/6/7.

So `mapping.ilo_generation` parses the trailing integer
(`re.search(r"(\d+)\s*$", ...)`) and reports a non-match as `None`
("unknown"), never as a guessed generation. Nothing branches on the
result — it is used only to make the aggregated unreadable-subresource
log say *which* hardware could not be read.

`mpFirmwareVersion`'s format is undocumented (conventionally
`2.78 Mar 15 2023`), so it is never parsed for a minimum-version gate.

`mpLicenseType` is worth knowing about even though it is unused: `null`
means "OneView encountered a problem while fetching the license type",
which is not the same as unlicensed.

## GPUs

OneView reports GPUs as `Devices` subresource entries with
`DeviceType: "GPU"`, alongside NICs, storage controllers and empty slots.
HPE's own example shows an empty bay as
`{"DeviceType": "Unknown", "Name": "Empty slot 2", "Status": {"State": "Absent"}}`,
so `Status.State == "Absent"` is filtered before anything is counted.

Available per GPU: `Name` (the model string), `Manufacturer`, `Location`,
`SerialNumber`, `PartNumber`, `FirmwareVersion.Current.VersionString`,
`Status`.

**There is no GPU memory field. Anywhere.** Not in `HpeServerDevice`, not
on `server-hardware`, not in the `Processor` schema. So `memory_bytes` is
always `None` from this collector, and VRAM comes entirely from
`GpuCatalog` (ADR-0021) keyed on the model string.

### HPE product names and the catalog

HPE rebrands NVIDIA cards, so `Name` is HPE's *product name*, not the
chip's model string: `"HPE NVIDIA L40S 48GB PCIe Accelerator"` where a
BMC reports `"NVIDIA L40S"`. Those did not match the catalog as it stood.

Matching was extended (ADR-0022, "GPU matching"): a leading `HPE`/`HP`
and a trailing marketing noun are dropped in normalization, and lookup
falls back through a trailing bus word (`PCIe`, `SXM4`) and then a
`<model> <N>GB` spelling against a bare-model row — **accepted only when
N GB equals that row's own VRAM**.

That capacity check is the safety property: `"HPE NVIDIA A16 64GB PCIe
Accelerator"` matches nothing, because this table models the A16 as the
four 16GB GPUs it carries. A disagreeing capacity produces an honest miss
that an operator fixes with `INVENTORY_GPU_MODELS`, never a confident
wrong number.

**UNVERIFIED:** the real strings this estate's appliances report. The
rules above were built against realistic spellings, not observed ones.
Probe section 7 prints every GPU string found with a CATALOG HIT/MISS
verdict — that is the list to act on.

**UNVERIFIED:** whether GPUs also appear under
`/rest/server-hardware/{id}/processors` as `ProcessorType: "GPU"`. If they
do, a future Redfish pass would get GPUs for free on iLO 5+.

## Power supplies

**The one per-server call, and the first time any provider in this repo
has populated `ProviderServer.psus`.** That field was added on
2026-09-01 and `IngestService` hardcoded `Power(psus=[])` for every
provider, so `power.psu_count` and `power.failed_psu_count` have had
nothing to read since they were written — a server with a dead PSU
reported HEALTHY on power exactly like one with two good ones.

`GET /rest/server-hardware/{id}/powerSupplies`, HPE's
`HpeServerPowerSupply` schema. What the mapper uses:

| Field | HPE's description |
|---|---|
| `PowerCapacityWatts` | "The maximum amount of power, in **Watts**, that the associated power supply is rated to deliver" |
| `Oem.Hpe.PowerSupplyStatus.State` | `Ok`, `Degraded`, `Failed`, `OverVoltage`, `OverCurrent`, `OverTemperature`, `ACPowerLost`, `FanFailure`, `WarningHighInputVoltage`, `GoodInStandby`, `Unknown`, … |
| `Status.Health` | the generic Redfish rollup |
| `Model`, `SerialNumber`, `MemberId` | free text |

**The HPE state decides health, not a boolean.** It is the more specific
answer — a PSU that lost AC input is a different operational fact from
one that is degraded from one that failed — so `Failed`, `ACPowerLost`,
`OverVoltage`, `OverCurrent`, `OverTemperature` and `FanFailure` map to
CRITICAL, `Degraded` and the voltage warnings to WARNING, `Ok` and
`GoodInStandby` to HEALTHY. A state this platform has no mapping for
falls back to `Status.Health`. `Status.State == "Absent"` is an empty bay
and contributes no PSU at all.

Two documentation notes worth keeping: `LineInputVoltage`'s own
description says "in Watts", **which is an error in HPE's doc** — it is
volts, and this collector does not read it; and
`Oem.Hpe.AveragePowerOutputWatts` is "usually updated every 10 seconds
but the period can vary".

### The undocumented gap, and why it is handled rather than assumed

`/powerSupplies` returns a `SubResourceV10` envelope but has **no
matching `SubResourceName` enum value** — the enum has exactly eight
(`AdvancedMemoryProtection`, `Devices`, `LocalStorage`,
`LocalStorageV2`, `MPSettings`, `Memory`, `MemoryList`, `Unknown`) and
`/powerSupplies`, `/thermal`, `/processors` and `/networkAdapters` are
none of them. So whether `expand=all` already returns power supplies is
undetermined in HPE's own documentation.

The collector reads the expanded payload first and only calls per server
for what is missing, then logs `oneview.power_supply_source` with
`from_expand` and `per_server_calls`. That difference is ~15 requests
versus one per server, and an undocumented gap must not turn one into the
other silently. `tools/verify_oneview.py` section 10 settles it.

A `/powerSupplies` call that fails, or reports a `collectionState` other
than `Collected`, yields `None` for that server — unread, carried
forward — and is counted into one aggregated
`oneview.power_supplies_unreadable` warning. It never aborts the run.

## What OneView does not have at all

Checked against the full `ServerHardwareV12` field list and every
documented subresource schema. Each of these is reported as `None` /
`UNKNOWN` and **never derived, inferred or defaulted**:

- **Thread / logical-processor count** at server level — only
  `/processors`, per server.
- **NIC link speed and link state** — absent from `portMap` entirely.
- **CPU or component temperature.** `/thermal` is **fans only**: the
  documented schema has five properties (`Name`, `Reading`,
  `ReadingUnits`, `Status`, `MemberId`) and no `Temperatures[]`, and
  `ReadingUnits`' only value is `Percent`, so there is no fan RPM either.
  The only temperature OneView has is `AmbientTemperature` in
  `/utilization` — **inlet air, in °C, and historical sampled data, not a
  current reading**. That is a different physical quantity from the
  per-component temperatures the Cisco collectors report, so it is not
  collected: putting an inlet sample into a component-temperature field
  would make two vendors' numbers look comparable when they are not. If
  inlet temperature is wanted later it needs its own field.
- **GPU memory** — see below.
- **Per-DIMM health** — only the aggregate `AmpModeStatus`.
- **Total storage capacity as a single field** — summed from the drives.

## Health fields, and one gotcha

`status` values are `OK` / `Disabled` / `Warning` / `Critical` /
`Unknown`, and **`Disabled` is also what an unassigned server reports**
("indicates that a resource is not operational *or that a server profile
has not been assigned*"). Mapping it straight onto a health state would
mark every spare server unhealthy. This collector does not map OneView's
`status` onto health at all — the health-policy engine works from
collected metrics — but the trap is recorded here because it is the
obvious thing for a future change to reach for.

`stateReason == "CommunicationError"` ("appliance cannot communicate with
iLO or OA") is the honest "OneView can't reach the iLO" signal and belongs
in `collection_errors`, not in a health verdict.

## One appliance, and the ceiling that would change that

`INVENTORY_ONEVIEW_IP` is a **single endpoint**, resolved through
`EnvConnectionResolver` like every other vendor's. There is no appliance
list, no per-appliance concurrency and no partial-success accounting:
one appliance failing is the run failing.

**The documented ceiling, recorded so nobody rediscovers it from a
truncated fleet.** HPE OneView 10.0 Support Matrix, docId
`sd00006056en_us`, "Configuration maximums":

| Resource | Maximum |
|---|---|
| Total servers per appliance | **2500** |
| …on a non-ESXi hypervisor | **1024** |
| Assigned server profiles | 2500 |
| **Unassigned** server profiles | **100** |
| Volumes per server profile | 512 |

> "The total number of servers in an HPE OneView **VM appliance cannot
> exceed 2500 servers if the VM OVA is deployed using a VMware vSphere
> ESXi hypervisor**. … **For hypervisors other than ESXi, the HPE OneView
> appliance can manage and monitor up to a maximum of 1024 servers.**"

This estate is one appliance and well inside that. **An estate that
outgrows it needs a second endpoint** — `INVENTORY_ONEVIEW_IP` becoming a
list, or a CronJob per appliance — and that is deliberately not built.
The symptom, if it is ever reached, is not a crash: it is
`oneview.collection_truncated` at ERROR, or a fleet that quietly stops
growing.

**Rate limits: not documented.** No requests-per-second, no concurrency
cap, no 429 in the response-code table (400, 401, 403, 404, 409, 410,
412, 415, 500, 503). The only hints are indirect — "OneView may limit the
number of resources returned" and 503 for "currently unable to handle the
request". `INVENTORY_ONEVIEW_PSU_CONCURRENCY` defaults to 8 for that
reason: one appliance is a single point of failure for up to 2500
servers, and ADR-0016's "embedded management hardware degrades when
polled" warning applies to it too.

## Why not the `hpeOneView` SDK

It is maintained (11.4.0, 2026-08-13) and small, so the Intersight reason
does not apply. It is rejected because the protocol is trivially small
while the SDK is synchronous (`http.client` directly — every call would
need `asyncio.to_thread`), depends on `future`, and pins `docutils<0.18`
across the whole air-gapped wheel mirror.

Four behaviours were taken from reading its source anyway, because they
are what a hand-rolled client learns the hard way:

| Behaviour | SDK evidence |
|---|---|
| `loginMsgAck` force-set on every login | `connection.py:468` |
| Default version = `GET /rest/version` → `currentVersion` | `connection.py:78-82` |
| Version validated against `[minimumVersion, currentVersion]` | `connection.py:85-93` |
| Page loop guards `nextPageUri == uri` | `resource.py:778` |

**Not** taken: its TLS default, which trusts any certificate unless a
bundle is passed. `INVENTORY_ONEVIEW_VERIFY_TLS` defaults off for the
stated reason that an air-gapped appliance ships a self-signed
certificate, and turning it on with a real chain is the scalable answer.
