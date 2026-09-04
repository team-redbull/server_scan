# HPE OneView REST API — research notes for a OneView-only collector

Scope: **OneView is the only source.** No per-host BMC connection, no
`RedfishTarget` handoff, no iLO-generation routing. Every field this platform
stores has to come out of the OneView appliance's own REST resources or it does
not exist for HPE hardware.

## What this settles / what it does not

**Settles.** The estate enumerates in **one paginated call** —
`GET /rest/server-hardware` returns the *full* `ServerHardwareV12` object per
member, not a trimmed summary (§12, verified by diffing the collection's field
set against the single-resource GET's: identical). That one call already
carries identity, serial, model, UUID, memory total, CPU counts, every NIC MAC,
the iLO address, power state and health. Adding `expand=all` folds in DIMMs,
drives, PCI devices and GPUs at **no extra call count**. Firmware for the whole
fleet is a second bulk call, `GET /rest/server-hardware/*/firmware`. `memoryMb`
is documented as MiB **with the conversion factor spelled out**, so the
Intersight `TotalMemory` trap does not repeat here. PSU detail is rich and
per-PSU. Ambient temperature and power draw exist, in documented units, via
`/utilization`. Gen8 and Gen9 hardware — the whole iLO-4 range — is **still in
OneView 10.0's supported-managed list**, so the decision's premise holds (§11).

**Does not settle, and one of these is the whole risk of the OneView-only
decision.** HPE states, on every subresource, that "the minimum version to
collect **some types of inventory** is iLO 5 v1.20" — and **never says which
types**. That single undocumented sentence decides whether an iLO-4 server
comes back with drives, DIMMs and GPUs or with an `InsufficientFirmware`
envelope and nothing in it (§11). Separately: **thread count does not exist**
anywhere in OneView except a per-server `/processors` call, **NIC link speed
and link state do not exist at all**, and **`/thermal` is fans only — a
percentage, with no temperature beside it** (§6.5, §6.6). And
`/rest/server-profiles` caps at 256 per request with `count=-1` meaning *64*,
which is the easiest way to ship a collector that sees a fraction of the fleet
(§2c).

Ten open items are listed at the end, each phrased as one query for
`tools/verify_oneview.py`.

## 0. Sources, and one disagreement between them

Two primary documents, both HPE's own API Reference, and they are **different
products, not different versions**:

| Doc | Flavour | OneView release | `X-API-Version` |
|---|---|---|---|
| [`dp00003271en_us`](https://support.hpe.com/docs/display/public/dp00003271en_us/index.html) | **for VMs** (non-Synergy: DL/ML rack, c-Class) | 8.00 | 4600 |
| [`dp00006616en_us`](https://support.hpe.com/docs/display/public/dp00006616en_us/index.html) | **for HPE Synergy** | 10.20 | 8000 |

Identified by TOC content, not by title — the VM doc has `#rest/rack-managers`
and no `#rest/interconnects`; the Synergy doc has `#rest/logical-enclosures`,
`#rest/fabrics`, `#rest/sas-interconnects`, `#rest/drive-enclosures` and no
`rack-managers`. **Our target is the VM flavour** (DL rack servers), but the
field tables below were read from the Synergy doc because it is the newer of
the two. Every field this note relies on was then confirmed to exist in the VM
doc at API 4600, and the highest-consequence descriptions (`name`, `memoryMb`,
`mpModel`, the `/rest/server-profiles` 64/256 wording) are **byte-identical
between them**, so nothing here is Synergy-specific.

**The disagreement:** the installed SDK states
`HPE OneView Python library extends support of the SDK to OneView REST API
version 8800 (OneView v11.40)` (`hpeoneview-11.4.0/README.md:25`), while the
newest API Reference reachable from this environment is API 8000 / OneView
10.20. HPE's own
[API Reference Quick Links](https://support.hpe.com/hpesc/public/docDisplay?docId=a00118111en_us&docLocale=en_US)
(Part Number 30-737BA5A1-114, Published August 2026) confirms an
`HPE OneView 11.4 API Reference` exists for both flavours, behind
`https://www.hpe.com/support/OneView-API-11-4-VM-EN` — **`www.hpe.com` is
unreachable from this sandbox** (connection times out), so 11.4's field tables
were not read. Per "contract over prose" the SDK is believed: API 8800 exists.
Nothing below depends on it; every cited field is stable across 4600 → 8000, a
span of ten releases.

**Reproducing these citations.** Both references are RequireJS single-page
apps; the hash fragments (`#rest/server-hardware`) fetch nothing a plain HTTP
client can read. The underlying page modules are at `<base>/<path>.html.js` —
e.g.
`https://support.hpe.com/docs/display/public/dp00006616en_us/rest/server-hardware.html.js`
(1.5 MB, one `define([], "…")` string of HTML). The user-guide and
support-matrix documents are a *different* system whose content API is
`https://support.hpe.com/hpesc/public/api/document/<docId>/toc` and
`…/<docId>/render?page=<page>.html`. Both anonymous, no login.

Citations below give `<doc>#<hash>` plus the exact field-table row name as the
page renders it, e.g. `members[] memoryMb`.

---

## 1. Authentication

Source: `dp00006616en_us#auth` and `#rest/login-sessions`.

**Model.** "The appliance uses a token-based authentication model. Under this
model, a user calls `POST /rest/login-sessions` to request a session token. A
session token is returned if the credentials passed in the POST request are
valid. The session token is passed in the header of each subsequent request"
(`#auth`, "Authentication").

**Login.** `POST /rest/login-sessions`.

| Request field | Type | Required |
|---|---|---|
| `userName` | string | required |
| `password` | string | required |
| `authLoginDomain` | string | optional — "Name of directory" |
| `loginMsgAck` | Boolean | optional — "When Require acknowledgement is enabled, this must be set to `true`" |

Response body (`Login Session Identifier new version`): `sessionID` (string,
"Session token used for authentication"), `partnerData` (any type).

```
POST https://{appl}/rest/login-sessions
X-Api-Version: 8000
Content-Type: application/json
{ "password":"mypassword", "userName":"administrator", "loginMsgAck": true }

HTTP/1.1 200 OK
{ "partnerData": {}, "sessionID": "abcdefghijklmnopqrstuvwxyz012345" }
```

`Content-Type: application/json` is `required`; any other value returns 415,
and absent it "`application/octet-stream` is assumed".

**Session token header name: `Auth`.** On every authenticated operation:
"`Auth` — Session authorization token obtained from logging in. If this header
is not included or if the session-token is invalid, the response code will be
401 Unauthorized." The SDK sends it lowercase —
`self._headers['auth'] = auth` (`hpeoneview-11.4.0/hpeOneView/connection.py:484`),
`conn.putheader('auth', …)` (`connection.py:314`) — same header, HTTP names
being case-insensitive. It is a bare token, **not** `Authorization: Bearer …`.
401 carries `errorCode: AUTHORIZATION`.

**Reconnect.** `PUT /rest/login-sessions` with the `Auth` header, "Reconnect to
an existing session that is neither explicitly logged out nor timed out".

**Logout.** `DELETE /rest/login-sessions` with `Auth` and `X-Api-Version` →
`HTTP/1.1 204 No Content`. The SDK does exactly this then
`del self._headers['auth']` (`connection.py:499-500`).

**Session lifetime and limits.** HPE OneView 10.2 User Guide for VMs, "Creating
a login session" (docId `sd00006562en_us`, page `s_security-session-atlas.html`):

- "A session remains valid until you log out or the session times out … **The
  default timeout value is 24 hours.** To change the value on a per-session
  basis, use `POST /rest/sessions/idle-timeout`. You can change the value to 24
  hours or less."
- `SESSION_CRITICAL_LIMIT` — "The maximum number of active user sessions, by
  default, is **2400**. All the remote (nonkiosk) logins are blocked once the
  number of active user sessions reaches this limit."
- `SESSION_CLIENT_LIMIT` — "The maximum number of active user sessions **from a
  particular IP address** by default is **960**."
- Both adjustable via `PUT https://{appliance}/rest/session-settings`.

A collector that logs in and never logs out leaks one session per run against
a 960-per-source-IP ceiling. `DELETE` in a `finally`.

### 1b. `X-API-Version`

**Required on every documented operation** (`integer`, `required`): "Specifies
the version of the API to invoke. The behavior of a given API version remains
the same. It is upward compatible from release to release. … To ensure expected
behavior, always provide the X-Api-Version value."

**Discoverable without credentials.** `#rest/version`:

```
GET https://{appl}/rest/version
Accept-Language: en_US
```
"Note that this request does not require Auth or X-Api-Version headers."
```
{ "currentVersion" : 8000, "minimumVersion" : 1 }
```

`currentVersion` = "The latest supported API version"; `minimumVersion` = "The
minimum supported API version".

**Support policy.** `#about`: "**HPE OneView supports an API version for two
years after its release.**" The 10.20 doc's table runs 8.50/5600 → 10.20/8000
and states "Any release prior to 8.50 (API version 5600) is not supported"; the
8.00 doc's runs 5.60/2400 → 8.00/4600. So an appliance accepts roughly two
years of older versions, not all of them.

| OneView | API | OneView | API |
|---|---|---|---|
| 8.50 | 5600 | 9.40 | 7400 |
| 8.60 | 5800 | 10.00 | 7600 |
| 8.70 | 6000 | 10.10 | 7800 |
| 8.80 | 6200 | 10.20 | 8000 |
| 8.90 | 6400 | *11.40* | *8800* (SDK README only) |
| 9.00–9.30 | 6600–7200 | | |

**What to pin.** Discover, don't hardcode: `GET /rest/version` unauthenticated
at run start, use `currentVersion` — exactly what the SDK does
(`get_default_api_version()`, `connection.py:78-82`), with `validateVersion()`
raising `Unsupported API Version` outside `[minimumVersion, currentVersion]`
(`connection.py:85-93`). Keep an `INVENTORY_ONEVIEW_API_VERSION` override so a
future `currentVersion` renaming something is a config change, not an outage.

**Invalid version → 412.** `#responseCodes`: "412 PRECONDITION FAILED … Also
returned when an invalid API version is sent in the `X-API-Version` header",
listed against `DELETE, PUT, PATCH` only — nothing about `GET`.

**UNVERIFIED: omission behaviour.** Documented `required`; the omitted case is
not documented anywhere. A silent fallback to an ancient version returns a
different schema rather than an error, which is the dangerous outcome. Open
question 5.

---

## 2. Enumerating the estate

### 2a. Pagination

Source: `#stdparams`, "Retrieving Large Collections of Resources" and the
`Collection Attribute` table. Every collection GET returns `start`, `count`,
`total`, `members`, `nextPageUri`, `prevPageUri`:

- `count` — "The actual number of resources returned. This value may be smaller
  than the `count` parameter specified in the GET invocation."
- `total` — "The total number of resources available in the requested
  collection, taking into account any filters."
- `nextPageUri` — "The URI that must be used to query for the next page … A
  null or empty `nextPageUri` indicates that the last page in the query has
  been returned."

"To ensure GET requests that return a list of resources respond in a timely
fashion, **OneView may limit the number of resources returned**" — the server
may return fewer than requested for its own reasons, so following
`nextPageUri` until null is the only correct loop, never `start += count`.

**Not a snapshot:** "Queries across multiple pages in a collection are
stateless and are based only on the start index and a count of resources
returned from that starting point *at the time the query is made*. For example,
if any server profiles were added or deleted after a GET operation is performed
… the returned page using the same nextpageURI may not contain the same set of
resources." A server added mid-sweep can be skipped or duplicated.
`IngestService` is idempotent on `(vendor, serial_normalized)` so a duplicate is
harmless; a skip is picked up next sweep.

**Copy the SDK's self-reference guard.** Its page loop has

```python
has_different_next_page = not response.get('uri') == response.get('nextPageUri')
```
(`hpeoneview-11.4.0/hpeOneView/resources/resource.py:778`, `get_next_page`).
That exists because an appliance has returned a self-referential
`nextPageUri`; without the guard the loop never terminates.

### 2b. `GET /rest/server-hardware`

`#rest/server-hardware`, first operation. "Gets a list of server hardware
resources. Returns a list of resources based on optional sorting and filtering,
and constrained by start and count parameters."

Query parameters: `count` (integer, **Default `-1`**, "A count of -1 requests
all the items"), `start` (integer, Default 0), `filter` (array of string),
`sort` (string), `expand` ("can have the value `all` or `none` … The value
`all` allows for the full expansion of sub resources data. The default value of
the expand parameter is `none`"). Supports `If-None-Match` → 304 and returns an
`ETag`. Authorization: `Action: Read`, `Category: server-hardware`, and
"**Access is not restricted by scope**" (`#auth`: "`Read` rights are NOT
restricted by scope").

**No documented maximum page size** for this resource — documented by absence,
unlike `/rest/server-profiles`. Treat `nextPageUri` as the contract and pass an
explicit `count` rather than relying on `-1`.

**Filtering works but the filterable field list is undocumented here.** The
only evidence is a worked example:

```
GET https://{appl}/rest/server-hardware?start=0&count=5&sort=position:desc&filter="serverProfileUri=null"
```

Do not read the `searchable` flag as "filterable": `#commonAttributes` defines
it as "Values in the field will show up in requests to the **index**, and the
search bar in the UI." Fields carrying `searchable` on server-hardware:
`maintenanceMode`, `model`, `mpLicenseType`, `name`, `position`, `powerState`,
`serialNumber`, `serverProfileUri`, `shortModel`, `state`.

`#stdparams` documents the grammar: `filter="[not] {attribute} {operator}
'{value}'"`, operators `= <> > >= < <= matches regex == smatches sregex sne`,
multiple `filter` params AND together, `matches` is SQL-wildcard (`%`, `_`) and
case-insensitive, `regex` is POSIX. Note "In some cases, `*` must be encoded as
`%25` on the URL." And the label: "**This parameter is experimental for this
release**: While generally functional when used in simple cases, restrictions
might be noted in the implementation description."

**Recommendation:** don't depend on server-side filtering for
`INVENTORY_COLLECTOR_NAME_PATTERN` — experimental by HPE's own label,
undocumented attribute set here, and the name we filter on is not on
`server-hardware` at all (§3a). Filter on the **profile** list, where the
attributes *are* enumerated, or in memory.

### 2c. `GET /rest/server-profiles` — the 256 cap

`#rest/server-profiles`, verbatim, and identical in the VM doc at API 4600:

> "Gets a list of server profiles based on optional sorting and filtering, and
> constrained by start and count parameters. **Providing a -1 for the count
> parameter will restrict the result set size to 64 server profiles. The
> maximum number of profiles is restricted to 256**, i.e., if user requests
> more than 256, this will be internally limited to 256. Filters are supported
> for the `name`, `description`, `serialNumber`, `uuid`, `affinity`, `macType`,
> `wwnType`, `serialNumberType`, `serverProfileTemplateUri`,
> `templateCompliance`, `status` and `state` attributes."

and: "If the number of profiles does not exceed the limit, then all profiles
are returned; otherwise, **the list is truncated**."

1. **Never send `count=-1` here.** It means 64.
2. Send `count=256` explicitly and follow `nextPageUri`.
3. **AMBIGUOUS.** "the list is truncated" does not say whether `nextPageUri` is
   populated past 256. Configuration max is 2500 assigned profiles (§8), so
   this is not hypothetical. Open question 2.

`GET /rest/server-profile-templates` carries the identical 64/256 wording, with
filters on `name`, `description`, `affinity`, `macType`, `wwnType`,
`serialNumberType`, `status`, `serverHardwareTypeUri`, `enclosureGroupUri`,
`firmware.firmwareBaselineUri`.

---

## 3. Identity fields

All rows from `#rest/server-hardware`, `GET /rest/server-hardware` →
`Response Body` → `ServerHardwareListV12` → `members[] …`, each confirmed
present in the VM doc at API 4600.

### 3a. The name — the UCS trap, verbatim

| Field | Documented description |
|---|---|
| `name` (string, searchable read only) | "**For blade servers, it is the location based name of the server, which is formed by concatenating the enclosure name and the bay number. For rack servers, it is the serial number prefixed by word "ILO" (e.g. ILOUSE31835LS).**" |
| `serverName` (string, read only) | "The name of the server **as reported by the iLO**. The iLO gets this information from a running operating system that has monitoring software installed, like Agentless Management Service." |
| *(profile)* `name` (string, required searchable, max 100) | "**Unique display name of this Server Profile.**" — `#rest/server-profiles` |

**`ProviderServer.name` must come from the server profile**, exactly as for
UCS. `server-hardware.name` is a location string carrying no site token and no
classifiable pattern — using it defeats `parse_site_code` and every
classification rule at once, which is precisely what ADR-0009 recorded for UCS
blades named after their chassis slot.

`serverName` is a decoy: OS hostname, only present where HPE Agentless
Management Service runs inside the OS. The docs state its *source* and say
nothing about the no-agent case. Open question 7.

A server with no profile has no usable name. Recommend skipping it — an
unassigned server is by definition not carrying an OCP workload, and the
alternative (`ILO<serial>`) parses to site `None`.

### 3b. Identity and model

| Field | Kind | Documented description |
|---|---|---|
| `serialNumber` | free text | "Serial number of the server hardware." — the `(vendor, serial_normalized)` correlation key |
| `uuid` | free text | "Universally Unique ID (UUID) of the server hardware." |
| `virtualSerialNumber` / `virtualUuid` | free text | "…associated with this server hardware (**if specified in the profile assigned to this server**)" |
| `model` | free text | "The full server hardware model string." |
| `shortModel` | free text | "Short version … typically something like `BL460 Gen8`." |
| `generation` | free text | "Generation of server." — **value shape not documented** |
| `formFactor` | free text | "For a blade server this is either `HalfHeight` or `FullHeight`. For a rack server this is expressed in U height, e.g. `4U`." |
| `platform` | free text | "Type of server (ex. `BladeServer`)." |
| `position` | measured int | "For blade servers, the number of the physical enclosure bay … **For rack mount servers, this value is null.**" |
| `assetTag`, `partNumber` | free text | |
| `uri` | free text | "The canonical URI of the resource" — the join key |
| `type` | free text | Default `server-hardware-12` — schema version marker |

**Do not use the profile's `serialNumber` for correlation.** It is "A 10-byte
value that is exposed to the Operating System as the server hardware's Serial
Number. The value can be a **virtual** serial number, user defined serial
number or physical serial number read from the server's ROM", with
`serialNumberType` defaulting to `Virtual`. A virtual serial splits one machine
into two documents.

### 3c. iLO address

| Field | Kind | Documented description |
|---|---|---|
| `mpHostInfo` | object | "The host name and IP address information for the Management Processor that resides on this server." |
| `mpHostInfo.mpHostName` | free text | "The host name of the Management Processor." |
| `mpHostInfo.mpIpAddresses` | array | "The list of IP addresses and corresponding type information for the Management Processor." |
| `…[].address` | free text | "An IP address for the Management Processor." |
| `…[].type` | enum `InetAddressType` | `DHCP`, `Static`, `SLAAC` ("Stateless address autoconfiguration (IPv6)"), `LinkLocal` ("Link-local address (IPv6)"), `LinkLocal_Required`, `Lookup` ("Address was obtained internally via host lookup (DNS, host table)"), `Undefined` |

Under the OneView-only design nothing connects to this address, so it is
**reporting data, not a connection input**. It still populates
`ProviderServer.bmc_address_raw` — what the UI shows and what a `BareMetalHost`
would round-trip — so pick deterministically: `Static` → `DHCP` → `Lookup`,
skipping `LinkLocal`/`SLAAC` (an IPv6 link-local address without a zone index
is not usable in a stored URL). Ordering, cardinality and IPv4/IPv6 mix are all
undocumented. Open question 6.

### 3d. Health, power state, lifecycle

| Field | Kind | Values / description |
|---|---|---|
| `powerState` | enum `PhysicalServerPowerState`, **required searchable** | `On`, `Off`, `PoweringOn`, `PoweringOff`, `Resetting`, `Unknown` ("Unable to determine server power state") |
| `status` | enum-ish string | "Overall health status of the resource … `OK` — indicates normal/informational behavior; **`Disabled` — indicates that a resource is not operational *or that a server profile has not been assigned*;** `Warning` — needs attention soon; `Critical` — needs attention soon; `Unknown` — should be avoided" |
| `state` | enum-ish string | `Unknown`, `Adding`, `NoProfileApplied`, `Monitored`, `Unmanaged`, `Removing`, `RemoveFailed`, `Removed`, `ApplyingProfile`, `ProfileApplied`, `RemovingProfile`, `ProfileError`, `Unsupported`, `UpdatingFirmware` |
| `stateReason` | enum-ish string | Meaningful only when `state == Unmanaged`. Includes `Unsupported`, `NotOwner`, `Unconfigured` ("discovery data incomplete or iLO configuration failure"), **`UnsupportedFirmware` ("iLO firmware version below minimum support level")**, **`CommunicationError` ("appliance cannot communicate with iLO or OA")** |
| `maintenanceMode` | Boolean | "When in maintenance mode, the appliance will not send any email notifications or forward SNMP traps related to the server hardware and its associated profile." |
| `refreshState` | enum `RefreshState` | `NotRefreshing`, `RefreshPending`, `Refreshing`, `RefreshFailed` |
| `uidState` | enum | `On`, `Off`, `Blinking`, `Unsupported` |
| `mpState` | enum | `OK`, `Resetting`, `Reset` (deprecated) |
| `mpLicenseType` | free text | "`""` — Indicates the management processor is not licensed. `Unknown` — Indicates OneView hasn't established communication with the management processor … **`null` — Indicates OneView encountered a problem while fetching the license type.**" |

**Gotcha:** `status == "Disabled"` is also what an unassigned server reports.
Mapping it straight onto a health state marks every spare server unhealthy;
pair it with `state`/`serverProfileUri`. `stateReason == CommunicationError` is
the honest "OneView can't reach the iLO" signal and belongs in
`collection_errors`, not in a health verdict — under a OneView-only design it
is the *only* signal we get that a server's data is stale.

---

## 4. The profile ↔ hardware link

**Hardware → profile.** `members[] serverProfileUri` (searchable read only):
"URI of a server profile assigned to this server hardware, if one is assigned.
**If not assigned this value is null.**"

**Profile → hardware.** `#rest/server-profiles`, `members[] serverHardwareUri`
(Format URI): "Identifies the server hardware to which the server profile is
currently assigned, if applicable."

**An unassigned profile** has `serverHardwareUri` and `enclosureBay`
("…currently assigned to, **if applicable**") absent or null. The docs do not
state which; treat both as unassigned.

**`associatedServer` is a trap.** "**The serial number** of the server hardware
that the server profile is currently applied to **or was most recently
assigned to**. This value is cleared if a different server profile is assigned
to the server hardware." A serial, not a URI, and *sticky* across
unassignment. Use `serverHardwareUri`.

Other profile fields we want: `uri` (the `profile_dn` analogue),
`serverProfileTemplateUri` → resolve against `/rest/server-profile-templates`
for `profile_template_name`/`_external_id`, `serverHardwareTypeUri`,
`enclosureGroupUri`/`enclosureUri`/`enclosureBay`, `status`, `state`
(`Normal`, `Creating`, `CreateFailed`, `Updating`, `UpdateFailed`, `Deleting`,
`DeleteFailed`), `templateCompliance`, `description`.

---

## 5. iLO generation — minor note

No longer drives a code path. Recorded because it is useful reporting and
because it predicts which servers report less (§11).

- `mpModel` (free text, read only): "The model type of the iLO, **such as
  `iLO4`**." One example, no enum, no pattern, nothing about iLO 6 or 7.
- `mpFirmwareVersion` (free text, read only): "The version of the firmware
  installed on the iLO." Format undocumented.
- `generation` (free text): "Generation of server." Value shape undocumented.
- `shortModel`: exemplified as `BL460 Gen8` — the more informative of the two.

Parse the trailing integer from `mpModel` for reporting
(`re.search(r"(\d+)\s*$", mpModel)`); treat a non-match as unknown. Open
question 4 collects the real value set in one query.

---

## 6. What OneView itself reports — the whole hardware surface

This is now the collector's entire data supply. For each field: **kind**
(measured number / enum / free text) and whether the **unit is documented** by
HPE or merely conventional.

### 6.1 The picture in one table

| `ProviderServer` field | OneView source | Extra calls |
|---|---|---|
| `cpu_sockets` | `processorCount` | none — in the list |
| `cpu_cores` | `processorCount * processorCoreCount` | none |
| `cpu_model` | `processorType` | none |
| `cpu_threads` | **only** `/processors` → `TotalThreads` | **1 per server** |
| `memory_total_bytes` | `memoryMb * 1048576` | none |
| DIMM detail | `Memory` subresource | `expand=all` |
| `storage_drives`, `storage_total_bytes` | `LocalStorage` / `LocalStorageV2` subresource | `expand=all` |
| `nic_macs`, `nics` | `portMap` | none |
| `gpus` | `Devices` subresource, `DeviceType == "GPU"` | `expand=all` |
| `psus` | `/powerSupplies` | **1 per server** |
| fans | `/thermal` | **1 per server** |
| temperature, power draw | `/utilization` (`AmbientTemperature`, `AveragePower`) | **1 per server** |
| firmware | `GET /rest/server-hardware/*/firmware` | **1 for the whole fleet** |

### 6.2 CPU

All four on the list object, no subresource.

| Field | Kind | Documented description | Unit |
|---|---|---|---|
| `processorCount` | measured int | "Number of processors installed on this server hardware." | n/a |
| `processorCoreCount` | measured int | "**Number of cores available per processor.**" | n/a |
| `processorSpeedMhz` | measured int | "Speed of the CPUs in **megahertz**." | **documented** |
| `processorType` | free text | "Type of CPU installed on this server hardware." | n/a |

**`processorCoreCount` is per-processor; this repo's `cpu_cores` is
whole-system.** The Redfish mapper populates it from
`ProcessorSummary.CoreCount` or the sum of every `Processors` member's
`TotalCores` (`backend/app/infrastructure/providers/redfish/mapping.py:285-290`).
So:

```
cpu_sockets = processorCount
cpu_cores   = processorCount * processorCoreCount     # NOT processorCoreCount
cpu_model   = processorType
```

Writing `cpu_cores=processorCoreCount` under-reports every 2-socket server by
half, silently, forever. This is the "field name right, unit wrong" failure the
brief warns about, and the easiest mistake to make here.

**Threads: OneView does not report logical processors at the server level.**
There is no `logicalProcessorCount`, `threadCount` or hyperthreading flag
anywhere in `ServerHardwareV12`. The only source is a per-server call:

`GET /rest/server-hardware/{id}/processors` → Redfish `Processor` schema →
**`TotalThreads`** ("The total number of execution threads supported by this
processor", measured int), alongside `TotalCores` ("The total number of cores
contained in this processor"), `MaxSpeedMHz` ("The maximum clock speed of the
processor", "in **MHz**" — documented), `Model`, `Manufacturer`, `Socket`,
`InstructionSet` (enum `x86`, `x86-64`, `IA-64`, `ARM-A32`, `ARM-A64`,
`MIPS32`, `MIPS64`, `OEM`), `ProcessorArchitecture`, `ProcessorType` (enum
`CPU`, `GPU`, `FPGA`, `DSP`, `Accelerator`, `OEM`), `ProcessorId.*`,
`Status.{Health,HealthRollup,State}`.

`/processors`'s `TotalCores` is a *cross-check* on
`processorCount * processorCoreCount` — worth asserting once in the probe.

**Recommendation:** report `cpu_threads=None` from the bulk path. The port
contract already means "not read this run" and `IngestService` carries the
previous value forward — strictly better than ADR-0020's rejected `2 x cores`
heuristic, which is exactly the class of guess this platform removed from the
Dell collector. Make the per-server `/processors` call opt-in behind a setting
if threads turn out to matter (§12 for the cost).

### 6.3 Memory

**Total, on the list object:**

> `memoryMb` — "Amount of memory installed on this server hardware in **MiB
> (1 MiB = 1,048,576 bytes)**." — integer, read only.

**The unit is documented, explicitly, with the conversion factor written
out.** This is the one place a vendor in this repo does better than the others:
contrast Intersight's `TotalMemory`, which carries no documented unit anywhere
and forced ADR-0017's assumption that leaves every server 4.86% high if wrong.
Here:

```
memory_total_bytes = memoryMb * 1_048_576
```

No verification run needed for the unit. (Whether an iLO-4 server populates it
at all is §11.)

**Per-DIMM, `Memory` subresource** (`GET …/{id}/memory`, or `expand=all`),
Redfish `Memory` schema:

| Field | Kind | Unit |
|---|---|---|
| `CapacityMiB` | measured number | "Memory Capacity in **MiB**" — documented |
| `OperatingSpeedMhz` | measured number | "Operating speed of Memory in **MHz**" — documented |
| `MemoryDeviceType` | enum | `DDR`, `DDR2`, `DDR3`, `DDR4`, `DDR4_SDRAM`, `DDR4E_SDRAM`, `LPDDR4_SDRAM`, `DDR3_SDRAM`, … |
| `BaseModuleType` | enum | `RDIMM`, `UDIMM`, `LRDIMM`, `SO_DIMM`, `Mini_RDIMM`, … |
| `MemoryType` | enum | `DRAM`, `NVDIMM_N`, `NVDIMM_F`, `NVDIMM_P` |
| `DeviceLocator` | free text | "Location of the Memory in the platform" |
| `MemoryLocation.{Socket,MemoryController,Channel,Slot}` | measured int | |
| `Manufacturer`, `PartNumber`, `SerialNumber`, `FirmwareRevision` | free text | |
| `ErrorCorrection` | enum | `NoECC`, `SingleBitECC`, `MultiBitECC`, `AddressParity` |
| `BusWidthBits`, `DataWidthBits` | measured number | bits — documented |
| `OperatingMemoryModes` | enum array | `Volatile`, `PMEM`, `Block` |

Maps onto `MemoryModule` directly: `slot` ← `DeviceLocator`, `size_bytes` ←
`CapacityMiB * 1_048_576`, `type` ← `MemoryDeviceType`, `speed_mhz` ←
`OperatingSpeedMhz`, `serial` ← `SerialNumber`. Note the schema has **no
per-DIMM health field** — `Status` is absent from the documented property set,
a gap versus Redfish. Memory *health* comes instead from
`AdvancedMemoryProtection`.

`MemoryList` is a separate subresource — "Lists each processor in the server
and the various memory statistics associated with each of those processors."

`AdvancedMemoryProtection` (`GET …/{id}/advancedMemoryProtection`, also in
`expand=all`) gives `AmpModeActive` (enum: `Unknown`, `Other`, `None`,
`Mirroring`, `OnlineSpare`, `RAIDXOR`, `AdvancedECC`, `Lockstep`, `A3DC`) and
`AmpModeStatus` (enum: `Protected`, `NotProtected`, `Degraded`, `DIMMECC`,
`DegradedMirroring`, `DegradedOnlineSpare`, `DegradedAdvancedECC`,
`DegradedLockstep`, `DegradedA3DC`, …) — where each "Degraded*" is documented
as "One or more DIMM faults have been detected." That is the closest thing
OneView gives to a memory health signal, and a natural health-policy metric.
Free with `expand=all`.

### 6.4 Local storage — which resource, exactly

**Not on `server-hardware`.** There is no `storageMb`, no
`localStorageCapacity`, no drive array on the `ServerHardwareV12` object. Local
storage exists **only** as a subresource, and there are **two mutually
exclusive schemas**:

> "Starting with Gen 10 Plus, certain storage adapters will provide
> **`/localStorageV2`** instead of (or in addition to) `/localStorage`. For
> Superdome Flex server types, storage inventory will be provided with
> `/localStorageV2` instead of `/localStorage`."
> — `#rest/server-hardware`, `GET /rest/server-hardware/{id}/localStorageV2`

Exact URIs: `GET /rest/server-hardware/{id}/localStorage` and
`GET /rest/server-hardware/{id}/localStorageV2`. Both are in the `subResources`
object, so **both come back with `expand=all` on the collection** — no
per-server call needed for either.

**`/localStorageV2`** — stock Redfish `Storage`. `Drives[]`:

| Field | Kind | Unit |
|---|---|---|
| `CapacityBytes` | measured int | "The size in **bytes** of this drive" — documented |
| `BlockSizeBytes` | measured int | bytes — documented |
| `MediaType` | enum | `HDD`, `SSD` — **only two values**, no NVMe |
| `Protocol` | enum | `NVMe`, `SATA`, `USB` — **this is where NVMe lives** |
| `RotationSpeedRPM` | measured int | "in revolutions per minute (RPM)" — documented |
| `NegotiatedSpeedGbs` / `CapableSpeedGbs` | measured int | "in **Gigabits per second**" — documented |
| `Model`, `SerialNumber`, `Manufacturer`, `PartNumber`, `Revision` | free text | |
| `FailurePredicted` | boolean | "Is this drive currently predicting a failure in the near future" |
| `PredictedMediaLifeLeftPercent` | measured int | percent — documented |
| `Status.{Health,State}` | enum | |
| `PhysicalLocation.PartLocation.{LocationType,LocationOrdinalValue,ServiceLabel}` | enum + int | `Slot`, `Bay`, `Connector`, `Socket` |
| `Identifiers[].{DurableName,DurableNameFormat}` | free text | |

Controllers: `CacheSummary.TotalCacheSizeMiB` ("measured in **MiB**",
`units: MiBy`), `PersistentCacheSizeMiB`, `FirmwareVersion`, `ControllerRates`.

**`/localStorage`** (v1, HPE SmartStorage). `PhysicalDrives[]` has **three
overlapping capacity fields**:

| Field | Documented description |
|---|---|
| `CapacityMiB` | "Total capacity of the drive in MiB" |
| `CapacityLogicalBlocks` | "Total number of logical blocks in the drive" |
| `BlockSizeBytes` | "Block size of the drive in bytes. This is the block size presented by the drive to clients" |
| `CapacityGB` | "Total capacity of the drive in GB. **This denotes the marketing capacity (base 10)**" |

Use `CapacityMiB * 1_048_576`, or `CapacityLogicalBlocks * BlockSizeBytes` for
an exact figure. **Never `CapacityGB`** — HPE says outright it is base-10
marketing. Media enum here is `HDD` / `SSD` / **`SMR`** ("shingled magnetic
recording") — one more value than V2. Also `InterfaceType`, `Model`,
`SerialNumber`, `RotationalSpeedRpm` ("only applicable on HDDs"),
`PowerOnHours` ("The number of lifetime hours that the drive has been powered
on"), `SSDEnduranceUtilizationPercentage`, `DiskDriveStatusReasons`. Controller
level: `CacheMemorySizeMiB`, `AdapterType` (`SmartArray`/`SmartHBA`/
`DynamicSmartArray`), `CurrentOperatingMode` (`RAID`/`HBA`/`Mixed`),
`BackupPowerSourceStatus` (`Present`, `NotPresent`, `PresentAndCharged`,
`PresentAndCharging`), plus a `LogicalDrives` tree.

**The mapper must handle both**, with different capacity conventions and
different media enums, and must derive `MediaType.NVME` from `Protocol` rather
than `MediaType` on V2. Compared with ADR-0020's Dell complaint — where
`serverArrayDisks` populated nothing usable and capacity had to be parsed out
of the model string — this is a large step up: both schemas give real capacity
in a stated unit. No heuristics needed.

### 6.5 NICs — `portMap`

On the list object, no subresource. `members[] portMap` (`ServerFabricMapV7`):
"A list of adapters/slots, their ports and attributes."

- `portMap.deviceSlots[]`: `deviceName` (free text, "**The name or model of the
  adapter**" — the per-adapter identity), `deviceNumber` (int), `slotNumber`
  (int, "The slot number of the adapter on the server hardware within its
  specified location"), `location` (enum `ServerFabricDeviceLocationEnum`:
  `Lom` "LAN on motherboard — fixed devices on ProLiant G7 series and later
  servers", `Flb` "Flexible LOM for Blades", `Flr` "Flexible LOM for Racks",
  `Mezz` "Mezzanine cards", `Ocp` "Open Compute Project", `Pci` "PCIe cards",
  `Unknown`).
- `…physicalPorts[]`: **`mac`** (free text, "Physical mac address of this
  physical port"), `portNumber` (int), `type` (enum `PhysicalServerPortType`:
  `Ethernet`, `FibreChannel`, `InfiniBand`, `SAS`), `wwn`, `interconnectUri`,
  `interconnectPort` ("If the adapter port is not connected to an interconnect
  downlink port, the value will be 0"), `physicalInterconnectUri`,
  `physicalInterconnectPort`, `nodeGuid`, `permanentNodeGuid`.
- `…physicalPorts[].virtualPorts[]`: "For Flex-capable devices, a list of
  **FlexNICs** defined on the server" — `mac`, `portNumber`, `portFunction`
  ("The function identifier for this FlexNIC, such as a, b, c or d"), `wwnn`,
  `wwpn`, `currentAllocatedVirtualFunctionCount`.

**Two hard limits, both by absence:**

1. **No link speed. No link state.** There is no `speedMbps`, no `linkStatus`,
   no up/down field anywhere in `portMap`. `ProviderNic.speed_mbps` must be
   `None` and `link_state` must be `"UNKNOWN"`. Do not synthesise either — a
   fabricated speed is worse than a missing one.
2. **`virtualPorts` are FlexNIC/partition MACs, not physical ports.** Feeding
   both levels into `nic_macs` inflates a correlation input. Use
   `physicalPorts[].mac` for `nic_macs`; FlexNIC MACs are detail.

Adapter membership is the `deviceSlots[]` nesting itself — each physical port
belongs to the slot it sits under, named by `deviceName` and located by
`location` + `slotNumber`.

`interconnectUri`/`interconnectPort` are the natural `ProviderAttachment`
source for blades in an enclosure, mirroring UCS fabric paths. Out of scope for
a first collector; noted so it isn't rediscovered.

**`/networkAdapters`** (`GET …/{id}/networkAdapters`) is a separate per-server
call returning the Redfish `NetworkAdapter` schema — `Controllers[]`,
`ControllerCapabilities` (`NetworkPortCount`, `NetworkDeviceFunctionCount`),
`Ports`/`NetworkPorts` (the latter marked "deprecated … in favor of the Ports
property"), `PCIeDevices`, `FirmwarePackageVersion`. It is **not** in the
`subResources` enum, so `expand=all` does not return it. Only worth a call if
port speed turns out to live under `Ports` — open question 8.

### 6.6 Power supplies, fans, temperature

**PSUs — `GET /rest/server-hardware/{id}/powerSupplies`, per-server call** (not
in the `subResources` enum). HPE `HpeServerPowerSupply` schema, and it is rich:

| Field | Kind | Unit |
|---|---|---|
| `PowerCapacityWatts` | measured number | "The maximum amount of power, in **Watts**, that the associated power supply is rated to deliver" — documented |
| `LastPowerOutputWatts` | measured number | "The average power output, measured in **Watts**" — documented |
| `LineInputVoltage` | measured number | "the value … of the line input voltage" — HPE's own description says "in Watts", **which is an error in the doc**; it is volts |
| `LineInputVoltageType` | enum | `ACLowLine` ("100-127V AC input"), `ACMidLine` ("200-240V"), `ACHighLine` ("277V"), `DCNeg48V`, `HighVoltageDC` ("380V"), `Unknown` |
| `PowerSupplyType` | enum | `AC`, `DC`, `Unknown` |
| `Status.{Health,HealthRollup}` | enum | `OK`, `Warning`, `Critical`, null |
| `Oem.Hpe.PowerSupplyStatus.State` | enum | `Ok`, `Degraded`, `Failed`, `OverVoltage`, `OverCurrent`, `OverTemperature`, `ACPowerLost`, `FanFailure`, `WarningHighInputVoltage`, … `GoodInStandby`, `Unknown` |
| `Oem.Hpe.AveragePowerOutputWatts` / `MaxPowerOutputWatts` | measured int | "(Watts). This is usually **updated every 10 seconds** but the period can vary" — documented |
| `Oem.Hpe.BayNumber` | measured int | |
| `Oem.Hpe.HotplugCapable`, `Mismatched`, `iPDUCapable` | boolean | |
| `Model`, `SerialNumber`, `PartNumber`, `SparePartNumber`, `Manufacturer`, `FirmwareVersion`, `Name`, `MemberId` | free text | |
| `Oem.Hpe.iPDU.{IPAddress,MacAddress,Model,SerialNumber,Id}` | free text | the PDU this PSU is plugged into |

Maps cleanly onto `ProviderServer.psus` / the `Psu` model, and is **better than
what the Cisco collectors get**. The health engine already has
`power.psu_count` and `power.failed_psu_count`; `Failed`, `Degraded` and
`ACPowerLost` are unambiguous inputs.

**Fans — `GET /rest/server-hardware/{id}/thermal`, per-server call.** And it is
**fans only**, despite the name:

| Field | Kind | Unit |
|---|---|---|
| `Name` | free text | "The name of the fan sensor" |
| `Reading` | measured int | "The current speed of the fan" |
| `ReadingUnits` | enum | **the only value is `Percent`** (plus null) |
| `Status` | enum | Redfish `Status` |
| `MemberId` | free text | |

**There are no temperature sensors in `/thermal`.** The Redfish `Thermal`
resource normally carries a `Temperatures[]` array; HPE's OneView projection of
it does not — the documented schema has exactly the five properties above. Fan
speed is a **percentage**, not RPM, and that is documented via the enum.

**Temperature and power draw — `GET /rest/server-hardware/{id}/utilization`,
per-server call, time-series.** This is where temperature actually lives:

| Metric (`fields=`) | Kind | Unit |
|---|---|---|
| `AmbientTemperature` | measured | "**Inlet air temperature in degrees Celsius** during this sample interval" — documented |
| `AveragePower` | measured | "Average power consumption in **Watts**" — documented |
| `PeakPower` | measured | "Peak power consumption in **Watts**" — documented |
| `PowerCap` | measured | "Dynamic power cap setting on the server hardware in **Watts**" — documented |
| `CpuUtilization` | measured | "CPU utilization of all CPUs in **percent**" — documented |
| `CpuAverageFreq` | measured | "Average CPU frequency in **Mhz**" — documented |
| `SdflexCpuUtilization` / `SdflexMemoryUtilization` | measured | percent; "supported only for Superdome Flex server types" |

"If unspecified, all metrics supported are returned." Query shape:
`?fields=AmbientTemperature&view=day` or
`?fields=CpuUtilization&filter=startDate=…&filter=endDate=…`; `view` takes
`hour`/`day` resolutions.

Two caveats. It is **inlet ambient temperature, not CPU or exhaust
temperature** — a different quantity from the per-component temperatures the
Cisco collectors report, and it should be labelled as such rather than dropped
into a generic "temperature" field. And it is **historical sampled data, not a
current reading**: "If the resource has no data, the UtilizationData is still
returned, but will contain no samples". A `refresh=true` parameter exists —
"Specifies that if necessary an additional request will be queued to obtain the
most recent utilization data from the iLO. **The response will not include any
refreshed data.**" — fire-and-forget with a `refreshTaskUri` to poll, useless
for a synchronous collector.

`GET /rest/server-hardware/{id}/environmentalConfiguration` returns
`EnvironmentalConfiguration`: `calibratedMaxPower` (integer, required — "the
maximum potential power that the device can consume … MUST represent the
maximum total AC input across all power supplies"), `capHistorySupported`, plus
location and dimensions. Static capability data, not a live reading.

### 6.7 GPUs

`GET /rest/server-hardware/{id}/devices` — **in the `subResources` enum, so
`expand=all` returns it.** HPE `HpeServerDevice` schema, keyed by `DeviceType`:

```
"enum": ["GPU", "PLX Controller", "Expansion Riser", "Smart Storage",
         "SAS/SATA Storage Controller", "IDE Storage controller",
         "USB Storage Controller", "Storage Controller", "LOM/NIC",
         "Converged Network Adapter", "Fibre Channel",
         "Direct Attached NVMe Device", "Backplane PIC",
         "Smart Storage Battery", "USB", "TPM",
         "Communication Controller", "Unknown"]
```

Per device: `Name` (free text, "Product Name"), `Manufacturer`, `Location`
(free text — `"PCI-E Slot 1"`, `"Embedded LOM"`, `"Embedded RAID"` in HPE's
example), `SerialNumber`, `PartNumber` ("Board part Number which is HPE PCA
Assembly Number"), `ProductPartNumber`, `ProductVersion`,
`FirmwareVersion.Current.VersionString`, `Status.{Health,State}` ("possible
values: `Absent`, `Enabled`"), `MCTPProtocolDisabled`.

**No GPU memory field. Anywhere.** Not in `HpeServerDevice`, not on
`server-hardware`, and the `Processor` schema's documented subset has no memory
property either. **This is exactly the `INVENTORY_GPU_MODELS` case** (commits
`9a75e35`, `693459c`): OneView gives a trustworthy model string and nothing
else, so `Gpu.memory_bytes` must come from the catalog keyed on `Name`. The
catalog will need HPE-branded SKU strings — HPE rebrands NVIDIA cards, and
those strings are not what Cisco reports for the same silicon, so plan a
normalisation pass rather than assuming existing keys hit.

**Filter on `Status.State == "Enabled"` before counting anything.** HPE's own
worked example shows empty slots present in the array as
`DeviceType: "Unknown"`, `Name: "Empty slot 2"`, `Status.State: "Absent"`, and
shows `Status: {"Health": null, "State": null}` on a populated NIC — nulls are
real.

`Devices` is also a second, independent source for storage controllers
(`Smart Storage`), NICs (`LOM/NIC`, `Converged Network Adapter`), NVMe
(`Direct Attached NVMe Device`) and TPM presence — all free with `expand=all`.

### 6.8 Firmware

**Fleet-wide, one paginated call:** `GET /rest/server-hardware/*/firmware` —
"Gets a list of firmware inventory across all servers." Response
`ServerFirmwareInventoryListV1`; each entry has `serverHardwareUri` ("URI of
the server hardware" — the join key), `serverName`, `serverModel`, and
`components[]` with `componentName` ("Name of the firmware component"),
`componentVersion` ("Installed version of the firmware component"),
`componentDescription`, `componentKey`, `componentLocation` ("Location of the
corresponding device like a slot"). All free text.

Documented server-side filters for this endpoint (a rare case where HPE
enumerates them): `components.componentName`, `components.componentVersion`,
`components.componentLocation`, `serverName`, `serverModel`. HPE's own examples
show real component names — `"iLO 5"`, `"HPE ProLiant System ROM"`,
`"Intelligent Provisioning"`, `"HPE Smart Array P408i-a SR Gen10"` — and real
version-string shapes, `"2.40 pass 5 Aug 13 2015"` and
`"I37 v2.20 (01/27/2016)"`.

**Per-server:** `GET /rest/server-hardware/{id}/firmware` →
`ServerFirmwareInventory`, same `components[]` shape plus `serverSettings`.

**On the list object, free:** `romVersion` (free text) — "The version of the
server hardware firmware (ROM). **After updating the ROM (BIOS) firmware for a
server, the server hardware page and the REST API may report an inaccurate ROM
version until the server is next powered on and allowed to complete the
power-on self-test (POST)**" — plus `mpFirmwareVersion` (the iLO's own version)
and `intelligentProvisioningVersion`.

`GET /rest/server-hardware/{id}/firmwareInventory` and `…/softwareInventory`
are additional per-server `SubResourceV10` endpoints.

### 6.9 What OneView does not have at all

Stated by absence, checked against the full `ServerHardwareV12` field list and
every documented subresource schema:

- **Thread / logical-processor count** at the server level — §6.2.
- **NIC link speed and link state** — §6.5.
- **CPU or component temperature** — only inlet ambient, via `/utilization`.
- **Fan RPM** — percent only.
- **GPU memory** — §6.7.
- **Per-DIMM health** — only the aggregate `AmpModeStatus`.
- **Total storage capacity as a single field** — must be summed from drives.

---

## 8. Scale, and the multi-appliance consequence

Source: **HPE OneView 10.0 Support Matrix**, docId `sd00006056en_us`
(Part Number 30-7DA43D79-010c, Published August 2025, Edition 4),
"Configuration maximums" → "Server hardware"
(`GUID-D7147C7F-2016-0901-066B-000000000529.html`) and "Server profiles"
(`GUID-D7147C7F-2016-0901-066B-00000000052A.html`).

> "The total number of servers in an HPE OneView appliance cannot exceed **2500
> servers**."

| Resource | Maximum |
|---|---|
| Total number of servers | 2500 |
| Managed servers | 2500 |
| Monitored servers | 2500 |
| Rack managers | 80 |
| Total assigned server profiles | 2500 |
| Total **unassigned** server profiles | **100** |
| Volumes per server profile | 512 |

with the deployment footnote:

> "The total number of servers in an HPE OneView **VM appliance cannot exceed
> 2500 servers if the VM OVA is deployed using a VMware vSphere ESXi
> hypervisor**. … **For hypervisors other than ESXi, the HPE OneView appliance
> can manage and monitor up to a maximum of 1024 servers.**"

**Consequence.** One appliance tops out at 2500 (1024 off ESXi), so a
10,000-server HPE estate would be at least four appliances.

**CORRECTED 2026-09-04 — this note previously recommended building the
collector multi-endpoint "from day one". That recommendation predates the
user's decision and is wrong for this deployment.** The estate has **one
OneView appliance and that is not going to change**, so
`INVENTORY_ONEVIEW_IP` is a single endpoint exactly like
`INVENTORY_OME_IP`, CLAUDE.md's "one endpoint and one login per
`ManagerType`" invariant is intact, and `EnvConnectionResolver` needed no
change. The 2500-server cap above is recorded as a documented limit, not
coded around: an estate that outgrows one appliance needs a second
endpoint, and `docs/adr/0022-oneview-only-hpe-collector.md` says so
rather than the code anticipating it. See also
`docs/adr/0012-env-manager-connections-and-one-manifest-set.md`.

**Sessions:** 2400 per appliance, 960 per source IP, 24-hour idle timeout (§1).

**Rate limits: not documented.** No requests-per-second, no concurrency cap, no
429 in `#responseCodes` (which lists 400, 401, 403, 404, 409, 410, 412, 415,
500, 503). The only hints are indirect: "OneView may limit the number of
resources returned … to ensure the GET requests respond in a timely fashion",
and 503 for "The server is currently unable to handle the request".

---

## 9. Testability without hardware

**No HPE equivalent of Cisco's UCS Platform Emulator could be confirmed as
publicly obtainable.**

1. **A 60-day OneView trial appliance.** HPE's developer portal
   (`https://developer.hpe.com/platform/hpe-oneview/home/`) links "Download a
   free trial"; the trial page itself
   (`https://www.hpe.com/*/resources/integrated-systems/oneview-trial.html`)
   was **unreachable from this sandbox** — `www.hpe.com` times out — so terms,
   format and licensing are `UNVERIFIED`. **The point stands regardless: it is
   a real appliance, not a hardware simulator.** With no HPE hardware to
   discover, `GET /rest/server-hardware` returns an empty collection. It would
   validate auth, versioning, pagination and error handling — more than
   Intersight ever got — and **zero** field mappings.
2. **HPE Synergy Data Center Simulator (DCS).** HPE community posts describe it
   as simulating "a datacenter comprised of multiple racks of HPE Synergy
   hardware … the same RESTful interface as real Synergy and OneView
   instances", and describe the demonstration appliance as "for internal and
   channel partner use only". **`UNVERIFIED` on both counts** —
   `community.hpe.com` returned HTTP 403 here, and DCS appears in no official
   HPE product documentation nor on `developer.hpe.com/blog/tag/simulator/`.
   It is also **Synergy** — blades, not the DL rack servers this estate runs.
3. **Workshops-on-Demand** (developer.hpe.com Hack Shack) — free Jupyter-based
   workshops against live appliances, session-limited; not usable by CI or a
   repeatable field test.

**Under the OneView-only design this matters more than it did.** With the split
design, the Redfish half was already validated (ADR-0016) and only the OneView
identity fields were new. Now every hardware field is new and unverified, and
the emptiness of the trial appliance means none of them can be checked without
the user's real appliance. `tools/verify_oneview.py` against live hardware is
not a nice-to-have here; it is the only verification that exists.

---

## 10. Python SDK — `hpeOneView`

| Package | Latest | Last release | Status |
|---|---|---|---|
| [`hpOneView`](https://pypi.org/pypi/hpOneView/json) | 5.3.0 | 2020-08-17 | dead (superseded) |
| [`hpeOneView`](https://pypi.org/pypi/hpeOneView/json) | **11.4.0** | **2026-08-13** | actively maintained |

`hpeOneView` metadata: "HPE OneView Python Library",
`https://github.com/HewlettPackard/oneview-python`, dependencies
`future>=0.15.2` and `docutils<0.18`. Cadence ~2 months: 10.1.0 (2025-07-02),
10.2.0 (2025-08-28), 11.0.0 (2025-12-17), 11.2.0 (2026-04-29), 11.3.0
(2026-06-24), 11.4.0 (2026-08-13). Read from `hpeoneview-11.4.0.tar.gz`
downloaded to the scratchpad and unpacked — **not installed into this
project**.

**It is synchronous:** `connection.py` imports `http.client` and `ssl` directly
(`connection.py:35,41`) and drives raw `conn.putheader(...)` calls
(`connection.py:314-318`). If we ever depended on it, CLAUDE.md's
`asyncio.to_thread` rule would apply as it does to `ucsmsdk`.

**Recommendation: hand-roll on `httpx`.** Different reason from Intersight —
that SDK was rejected for size (57.6 MB, 10,112 generated modules). This one is
small; it is rejected because the protocol is trivially small (one POST for a
token, one header, a handful of paginated GETs) and because a synchronous,
`future`-dependent package with a `docutils<0.18` pin is a poor thing to add to
an air-gapped wheel mirror.

**Behaviours worth carrying over from the SDK source:**

| Behaviour | Evidence |
|---|---|
| Session header is literally `auth` on the wire | `connection.py:314,484,500` |
| Default API version = `GET /rest/version` → `currentVersion` | `connection.py:71,78-82` |
| Version validated against `[minimumVersion, currentVersion]` before login | `connection.py:85-93` |
| `loginMsgAck` force-set to `True` — "This will handle the login acknowledgement message" | `connection.py:468` |
| Login accepts an existing `sessionID` and reconnects via `PUT` | `connection.py:472-478` |
| Pagination follows `nextPageUri` and **guards against `nextPageUri == uri`** | `resources/resource.py:755-784` |
| `get_all(count=-1)` means "keep following pages" client-side | `resources/resource.py:781` |
| SDK targets API 8800 / OneView 11.40 | `README.md:25` |

**Do not copy the SDK's TLS default.** It trusts any certificate unless an
`sslBundle` is passed (`self._sslTrustAll = True`, `connection.py:61-63`).
Follow ADR-0020's stated intent instead — a CA bundle with verification on, any
opt-out named and reasoned per the Redfish collector's `verify_tls_reason`
rule.

`endpoints-support.md`, referenced by the README but not shipped in the sdist,
is the SDK's own map of implemented endpoints — worth a look before an
appliance exists.

Notable gap: `server_hardware.py` has helpers for `get_local_storage()` (which
builds `"{}/localStorageV2"`,
`hpeoneview-11.4.0/hpeOneView/resources/servers/server_hardware.py:331`),
`get_bios()`, `get_firmware()`, `get_environmental_configuration()` — and
**none for `/devices`, `/memory`, `/processors`, `/powerSupplies`, `/thermal`
or `/networkAdapters`**. Those are hand-rolled either way.

---

## 11. Does OneView's data fidelity vary by iLO generation?

**The good news, and it is load-bearing: iLO-4 hardware is still supported.**
HPE OneView 10.0 Support Matrix, "Managed ProLiant DL rack mount servers"
(`sd00006056en_us`, page `GUID-D7147C7F-2016-0901-066B-00000000046D.html`) —
"The following rack server models can be added as **managed**", with columns
`Gen8 | Gen9 | Gen10 | Gen10 Plus | Gen10 Plus v2 | Gen11 | Gen12`:

| Model | Gen8 | Gen9 | Gen10 | Gen10 Plus | Gen10 Plus v2 | Gen11 | Gen12 |
|---|---|---|---|---|---|---|---|
| DL360 | | ✓ | ✓ | ✓ | | ✓ | ✓ |
| DL380 | | ✓ | ✓ | ✓ | | ✓ | ✓ |
| DL360p / DL360e | ✓ | | | | | | |
| DL380p / DL380e / DL380z | ✓ | | | | | | |
| DL385p | ✓ | | | | | | |
| DL560 | ✓ | ✓ | ✓ | | | ✓ | |
| DL580 | ✓ | ✓ | ✓ | | | | |

Gen8 boxes appear under their Gen8-era marketing names (`DL360p Gen8`, not
`DL360 Gen8`), which is why the `DL360` row has no Gen8 tick — not a gap. Both
iLO-4 generations, Gen8 and Gen9, are in the managed list of a current OneView
release. **The premise of the OneView-only decision holds.**

**The bad news is that HPE documents collectability, never fidelity.** The one
and only generation-conditional statement in the entire API reference is a
`CollectionState` enum value, repeated verbatim on every single subresource
endpoint:

> `InsufficientFirmware` — "The iLO firmware on the server is too low to
> collect the inventory. **The minimum version to collect some types of
> inventory is iLO 5 v1.20.**"

"**some types**" is the whole problem. HPE does not say which. Read literally,
an iLO-4 server may return `InsufficientFirmware` for `Memory`, `Devices`,
`LocalStorage` and `LocalStorageV2` — which is DIMMs, GPUs and every drive.
That would leave an iLO-4 server with only the top-level fields: `memoryMb`,
`processorCount`, `processorCoreCount`, `processorType`, `portMap`,
`serialNumber`, `model`, `mpHostInfo`, `powerState`, `status`. Identity and
coarse hardware, no component detail.

Whether the top-level fields themselves survive is **also undocumented** — they
are not subresources, so `collectionState` does not apply to them, and nothing
in the reference says where they are sourced from. That is the single most
important unknown in this research.

Two more generation-conditional notes, both documented, both narrower:

- **`localStorageV2` vs `localStorage` splits on adapter generation:**
  "Starting with Gen 10 Plus, certain storage adapters will provide
  `/localStorageV2` instead of (or in addition to) `/localStorage`." So an
  iLO-4 estate is `/localStorage` (v1 schema, `CapacityMiB`, `SMR` media
  value); a Gen10 Plus+ estate may be either or both. The mapper must handle
  both regardless.
- **`serverName` and `hostOsType` depend on an in-OS agent**, not on iLO
  generation — "The iLO gets this information from a running operating system
  that has monitoring software installed, like Agentless Management Service."
  Independent axis, same class of surprise.

Other `CollectionState` values, which under a OneView-only design are the only
honesty signal we have:

| Value | Documented meaning |
|---|---|
| `Collected` | "The data was successfully collected from the iLO and was current at the time of collection." |
| `CollectedStale` | "…but the data may be out of date or missing due to the server state. **This typically happens when the server is powered off or in POST.**" |
| `CollectionError` | "An error occurred during the collection of the data and it may be incorrect. **When an error occurs collecting the data and there is already data in OneView, the existing data is not overwritten.**" |
| `NotCollected` | "The initial state of a subresource when a server is added, before any inventory collection has been done." |
| `Unknown` | "Unable to determine server inventory collection state or null returned from the server." |

**Anything other than `Collected` must map to `None`, never zero.** This is the
exact bug the port's `None` contract exists to prevent — ADR-0016's case where
a 404'd `Storage` collection wrote zero drives and took a server from CRITICAL
to HEALTHY, because zero drives means zero failed drives. `CollectionError` is
the sharpest instance: OneView keeps its own stale copy but tells us it may be
wrong, so writing it as fact is worse than writing nothing.

Open question 1, and it belongs at the top of the probe.

---

## 12. Collection representation and cost model

### 12.1 The list returns the full object — verified

**`GET /rest/server-hardware` returns the complete `ServerHardwareV12` object
per member, not a summary.** Verified by extracting the field-name set from the
collection's `members[] …` table and from `GET /rest/server-hardware/{id}`'s own
`Response Body` table and diffing them: **every field in the collection member
exists in the single-resource GET, with no field present in only one.** Both
operations name the same schema — `ServerHardwareListV12` whose `members` is
"array of `ServerHardwareV12`", and `ServerHardwareV12` respectively.

So the following need **zero** per-server calls:

`name`, `serialNumber`, `uuid`, `model`, `shortModel`, `generation`,
`formFactor`, `platform`, `position`, `assetTag`, `partNumber`, `memoryMb`,
`processorCount`, `processorCoreCount`, `processorSpeedMhz`, `processorType`,
`portMap` (every adapter, port and MAC), `mpHostInfo` (iLO address), `mpModel`,
`mpFirmwareVersion`, `mpLicenseType`, `mpState`, `romVersion`,
`intelligentProvisioningVersion`, `powerState`, `status`, `state`,
`stateReason`, `maintenanceMode`, `refreshState`, `uidState`,
`serverProfileUri`, `serverHardwareTypeUri`, `serverGroupUri`, `locationUri`,
`scopesUri`, `signature`, `serverSettings`, `subResources` (metadata),
`virtualSerialNumber`, `virtualUuid`, `serverName`, `hostOsType`, `uri`,
`eTag`, `created`, `modified`.

There is a `view` parameter in the general parameter set (`#stdparams`:
`expand` / `summary` / `deep`) but `GET /rest/server-hardware` does **not**
list it among its query parameters — only `count`, `expand`, `filter`, `sort`,
`start`. Nothing forces a trimmed representation.

### 12.2 `expand=all` — subresources at no extra call count

`members[] subResources` holds, per subresource,
`{collectionState, count, data, etag, modified, name, uri}` (`SubResourceV10`).
On `data`:

> "When performing a GET of the full server-hardware object, the subresource
> metadata fields will be populated, but **the data field will be empty** to
> avoid sending large amounts of unwanted data. If you want the data field
> fully populated, **use the `expand=all` parameter** when performing the GET
> of the server-hardware object. When performing a GET of the subresource
> directly, the data field will always be populated."

and HPE documents the call explicitly:

```
GET https://{appl}/rest/server-hardware?expand=all
Auth: abcdefghijklmnopqrstuvwxyz012345
X-Api-Version: 8000
```

**The `subResources` object has exactly eight documented keys**
(`SubResourceName` enum): `AdvancedMemoryProtection`, `Devices`,
`LocalStorage`, `LocalStorageV2`, `MPSettings`, `Memory`, `MemoryList`,
`Unknown`.

So `expand=all` buys, for free in call count: **DIMMs, drives (both schemas),
PCI devices including GPUs, memory-protection status, and MP settings.**

Cost is response size — HPE's own stated reason for the default being `none`.
Page small (`count=25..50`) when using it.

### 12.3 What is genuinely per-server

Twelve endpoints return `SubResourceV10` but only eight names exist in the
`SubResourceName` enum. `/powerSupplies`, `/thermal`, `/processors` and
`/networkAdapters` return that envelope while having **no corresponding enum
value** — checked on each endpoint's own page, all four render the same
eight-value enum. They are also absent from the `subResources` object on
`server-hardware`. Either the enum is stale or those responses report
`name: "Unknown"`; either way **`expand=all` does not return them**. This is a
documentation inconsistency, and open question 3 settles it in one query.

Per-server-only:

| Endpoint | Gives | Needed for |
|---|---|---|
| `/powerSupplies` | full PSU array | `ProviderServer.psus` |
| `/thermal` | fan speeds (%) | fans |
| `/processors` | `TotalThreads`, per-socket detail | `cpu_threads` |
| `/networkAdapters` | Redfish `NetworkAdapter` | possibly port speed |
| `/utilization` | ambient temp, power draw, CPU% | environmental |
| `/bios` | `ServerBiosSettings` | not needed |
| `/environmentalConfiguration` | calibrated max power, location | not needed |
| `/firmware` | per-server firmware | superseded by the bulk call |
| `/physicalServerHardware` | "Applicable only for 'Superdome X' and 'Superdome Flex'" | not needed |
| `/chassis`, `/softwareInventory`, `/firmwareInventory` | misc | not needed |

**Fleet-wide alternative that avoids N calls:**
`GET /rest/server-hardware/*/firmware` — the whole estate's firmware inventory,
paginated, joined on `serverHardwareUri` (§6.8). There is **no** equivalent
wildcard endpoint for PSUs, thermal or processors.

### 12.4 Cost model, per appliance and per sweep

Let `N` = servers on one appliance (≤2500), `P` = page size.

| Tier | Calls | Yields |
|---|---|---|
| **A — baseline** | `1` login + `⌈N/P⌉` + `⌈profiles/256⌉` + `1` logout | names, serials, models, UUIDs, memory total, CPU counts, all MACs, iLO addresses, power, health |
| **B — + `expand=all`** | same call count, larger bodies | + DIMMs, drives, GPUs, PCI devices, AMP status |
| **C — + firmware** | `+ ⌈N/P⌉` | + full firmware inventory |
| **D — + PSUs** | `+ N` | + `ProviderServer.psus` |
| **E — + threads** | `+ N` | + `cpu_threads` |
| **F — + fans / environmental** | `+ 2N` | + fan speed, ambient temp, power draw |

At `N = 2500`, `P = 500`: **tier C is ~15 requests**; tier D ~2515; tier F
~7515. At the platform's 10,000-server target across four appliances: **tier C
≈ 60 requests total**; tier F ≈ 30,000.

**Recommendation: build tier C, make D opt-in.** Tier C is two orders of
magnitude cheaper than ADR-0020's Dell design (~25 round trips *per server*)
and covers everything `ProviderServer` needs except `psus`, `cpu_threads` and
environmental data. Tier D is the one worth paying for — `psus` is currently
hardcoded to `Power(psus=[])` by `IngestService` for every provider, and PSU
health is a real health-policy input. It also fits comfortably: 2500 sequential
requests against an appliance with a documented 960-session-per-IP ceiling and
no documented rate limit, on a 6-hourly CronJob.

**Concurrency has no documented ceiling** (§8), so it has to be measured — open
question 9. Start conservative: ADR-0016's warning that embedded management
hardware degrades when polled applies to the appliance too, and here one
appliance is a single point of failure for 2500 servers rather than one BMC for
one.

`If-None-Match`/`ETag` is supported on the collection GET and on every
subresource, so a future optimisation is conditional requests across sweeps —
304 responses cost nothing. Not worth building first.

---

## Open questions / UNVERIFIED — the `tools/verify_oneview.py` probe

Each phrased as one query, ordered by how much damage a wrong guess does.
Together they are a complete probe.

1. **Does an iLO-4 (Gen8/Gen9) server return real hardware data, or an empty
   `InsufficientFirmware` envelope — and do the top-level fields survive?**
   (§11) This decides whether the OneView-only design delivers hardware
   inventory for a third of the fleet or identity only. HPE says only "some
   types of inventory" need iLO 5 v1.20 and never says which.
   **Query:** `GET /rest/server-hardware?expand=all&count=25`; for a member
   whose `mpModel` reports iLO 4, print `mpModel`, `memoryMb`,
   `processorCount`, `processorCoreCount`, `len(portMap.deviceSlots)` and
   `{k: v["collectionState"] for k, v in subResources.items()}`. Repeat for a
   Gen10/Gen11 host and diff. **This is the probe's headline output.**

2. **Is the 256 cap on `/rest/server-profiles` per request or per query?**
   (§2c) If per query, we cannot enumerate >256 profiles and the "profile
   supplies the name" design needs another route. Config max is 2500.
   **Query:** `GET /rest/server-profiles?start=0&count=256` → read `total` and
   `nextPageUri`; GET that `nextPageUri`; assert the member `uri` sets are
   disjoint.

3. **Do `/powerSupplies`, `/thermal`, `/processors`, `/networkAdapters` come
   back with `expand=all`, or only as per-server calls?** (§12.3) The
   difference between tier C and tier F — ~60 requests versus ~30,000 for a 10k
   estate. The docs are self-inconsistent: those endpoints return
   `SubResourceV10` but have no `SubResourceName` enum value.
   **Query:** `GET /rest/server-hardware?expand=all&count=1` → print
   `sorted(members[0]["subResources"].keys())` and check for `PowerSupplies` /
   `Thermal` / `Processors` / `NetworkAdapters`.

4. **What does `mpModel` actually contain, per generation, and what is the real
   generation mix of this estate?** (§5, §11) Also tells us how much question 1
   matters.
   **Query:** `GET /rest/server-hardware?count=-1` →
   `sorted({(m["mpModel"], m["generation"], m["shortModel"]) for m in members})`
   plus a count per `mpModel`.

5. **What happens when `X-Api-Version` is omitted?** (§1b) Documented
   `required`; omission behaviour undocumented. A silent fallback to an old
   version returns a different schema, not an error.
   **Query:** the same `GET /rest/server-hardware?count=1` twice, with and
   without the header; compare status code and `members[0]["type"]`
   (`server-hardware-12` at API 8000).

6. **How many `mpIpAddresses` entries does a real DL report, in what order, and
   of what types?** (§3c) Determines the `bmc_address_raw` pick.
   **Query:** `GET /rest/server-hardware?count=5` →
   `[m["mpHostInfo"] for m in members]` verbatim.

7. **Is `serverName` populated without HPE AMS running?** (§3a) Only matters if
   we want a name fallback for profile-less servers.
   **Query:** in the same `count=5` response, print
   `[(m["name"], m["serverName"], m["serverProfileUri"]) for m in members]`.

8. **Does `/networkAdapters` carry port link speed or link state?** (§6.5)
   `portMap` has neither, so `ProviderNic.speed_mbps` stays `None` unless this
   endpoint has it. The Redfish `NetworkAdapter` `Ports` array normally carries
   `CurrentSpeedGbps` and `LinkStatus`, but HPE's documented property subset
   does not show them.
   **Query:** `GET /rest/server-hardware/{id}/networkAdapters` on one host;
   grep the `data` for `CurrentSpeedGbps`, `LinkStatus`,
   `LinkNetworkTechnology`.

9. **What concurrency does an appliance tolerate?** (§8, §12.4) Nothing
   documented; no 429 in the response-code table.
   **Query:** issue `GET /rest/server-hardware/{id}/powerSupplies` for 50 hosts
   at concurrency 1, 4, 8, 16; record wall-clock and any 503s. Only needed if
   tier D is built.

10. **Do the API 8800 (OneView 11.4) field tables differ?** (§0) The newest
    reference could not be fetched (`www.hpe.com` unreachable here). Every
    field cited is stable across API 4600 → 8000, so risk is low but not zero.
    **Query:** `GET /rest/version` on the real appliance tells us which version
    it speaks; if it is >8000, open
    `https://www.hpe.com/support/OneView-API-11-4-VM-EN` from a machine with
    normal internet and diff `#rest/server-hardware` against §6.

**One assertion worth adding to the probe regardless:** on any host,
`processorCount * processorCoreCount` from the list should equal
`sum(TotalCores)` from `GET /rest/server-hardware/{id}/processors`. If it does
not, the §6.2 core-count mapping is wrong and every HPE server ships a wrong
core count.
