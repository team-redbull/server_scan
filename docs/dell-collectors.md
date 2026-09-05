# Dell collectors — verified implementation facts

This is the technical reference behind
`app.infrastructure.providers.openmanage` (`client.py`, `mapping.py`,
`provider.py`). Every docstring in those modules points here rather than
carrying its own explanation, so the `##` headings below are load-bearing:
renaming one breaks the cross-references in the source files.

It is for whoever is about to change the Dell collector. **Provenance
matters here more than usual, and it is unusually weak.** Unlike the Cisco
collector — validated end to end against a live UCS Platform Emulator (see
`docs/adr/0009`) — the OME field names, endpoint paths and unit
assumptions below are carried over from a **production Dell scanner the
user ran in a separate project** (`dell_provider_example.py`, handed to
this repo as the starting point). They are real and worked in production,
but they have **not** been re-validated against a live OME appliance from
*this* codebase. Treat every "Size is MB" and status-code mapping as an
assumption to confirm on first contact with real hardware, and update this
document with what that proves — the same discipline the Cisco ADRs
record.

## OME REST surface

One OpenManage Enterprise appliance manages the whole Dell estate and
answers an OData-shaped REST API at `https://<appliance>/api`. The
collector uses four endpoints, all validated in the production scanner:

- `POST /SessionService/Sessions` — authenticate. The body is
  `{"UserName", "Password", "SessionType": "API"}`; the session token
  comes back in the **`X-Auth-Token` response header** (not the body), and
  every subsequent request must carry it. The body's `Id` is the session
  handle used to `DELETE /SessionService/Sessions('<id>')` on logout.
- `GET /ProfileService/Profiles` — one entry per server profile. An
  undeployed profile has no `TargetName`; it names no server and is
  counted and skipped, not reported as an unreachable host.
  `ProfileName` is the server's operator-facing name (and the platform's
  site/classification source); `TargetName` is the server's iDRAC IP.
- `GET /DeviceService/Devices` — one entry per managed device. `DeviceName`
  is the iDRAC IP (the join key back to a profile's `TargetName`), `Model`
  is the hardware model, `DeviceServiceTag` is the service tag used as the
  server serial, and `Id` is the device handle for inventory calls.
- `GET /DeviceService/Devices(<id>)/InventoryDetails('<section>')` — one
  hardware section for one device, returned in an `InventoryInfo` array.
  The collector reads `serverProcessors`, `serverMemoryDevices`,
  `serverArrayDisks` and `serverNetworkInterfaces`. OME populates these from
  the server's iDRAC, so "collect from OpenManage and iDRAC" is one REST
  surface, not two connections.

  **Physical disks are `serverArrayDisks`, not `serverStorage`.** A live
  appliance returns HTTP 400 for `serverStorage` — it is not a valid
  `InventoryType`. This was an assumption carried over from the production
  scanner that proved wrong here (2026-08-18, live OME run).

Collections are paged: each response carries a `value` array and, when
more remain, an `@odata.nextLink` (an absolute `/api/...` path). The
client follows that link rather than managing `$skip`/`$top` itself, so
the appliance stays authoritative on page size.

## Session lifecycle

`OmeClient` is an async `httpx.AsyncClient` used as an async context
manager: `__aenter__` logs in, `__aexit__` deletes the session and closes
the pool, best-effort (a failed logout is logged, never raised). One
session per collector run, never pooled across runs — the same shape the
Cisco clients use. TLS verification defaults **off** because air-gapped OME
appliances ship a self-signed certificate with no private CA to trust it
against; set `INVENTORY_OME_*` with a real chain and pass `verify_tls=True`
where one exists.

## Collection flow

**OME discovers, Redfish collects** — see
`docs/adr/0020-dell-identity-from-ome-hardware-from-redfish.md` for why,
and for what the OME-only design got wrong.

`OpenManageProvider.list_servers` makes the two bulk calls once
(`/ProfileService/Profiles`, `/DeviceService/Devices`) and joins each
profile to its device by iDRAC IP. That yields, per server, the four things
only OME knows: the **profile name** (the server's name), the **deployment
template**, the **service tag** and the **iDRAC address**.

Every matched address then becomes a `RedfishTarget`, and the hardware pass
is `app.infrastructure.providers.redfish` unchanged — CPU, memory, storage,
NICs and GPUs read from the server's own BMC as measured values.
`RedfishTarget.name` carries the OME profile name through to
`system_to_provider_server(override_name=...)`, which is what keeps a Dell
server named the thing site parsing and classification need rather than
whatever iDRAC calls it.

`name_pattern` is applied **before any BMC is contacted**. The expensive
pass is per-server here, so this is what keeps the design affordable; it is
still only an efficiency gate, with the authoritative filter remaining
`tools.run_collector._NameFilteredProvider`. Note this differs from the
standalone Redfish collector, where the pattern is deliberately *not*
applied because a BMC does not know the server's name — here OME supplies
it.

A profile OME reports no iDRAC address for is dropped and recorded: there
is nothing to collect it from. That and every per-host Redfish failure land
on `collection_errors`, which `tools.run_collector` reads to report the run
as **PARTIAL** (exit 3) rather than a silently-complete success.

Correlation keys on `(vendor, serial)`
(`app.application.services.ingest`). iDRAC reports the service tag as
`SerialNumber` — the same value OME reports as `DeviceServiceTag` — so
servers ingested by the older OME-only collector update in place rather
than duplicating. **This is the highest-consequence unverified assumption
in the design; confirm it on real hardware before a production run.**

The BMC address stored is OME's `idrac-virtualmedia://` form, not the
`https://<host>` origin the Redfish collector reports for a standalone BMC:
that is what `parse_bmc_address` documents for Dell and what a Metal3
`BareMetalHost` round-trips.

## Profile template

An OME server profile carries the deployment template it was created from
in its `/ProfileService/Profiles` entry: `TemplateName` and `TemplateId`.
The collector maps those onto `ProviderServer.profile_template_name` /
`profile_template_external_id`, which the ingest pipeline stores as the
server's `ProfileTemplate`. A profile deployed without a template leaves
both `None`. The field names are the usual **unverified assumption** —
confirm `TemplateName`/`TemplateId` exist on the profile object of the OME
version in use.

## BMC address

The iDRAC IP is rendered as
`idrac-virtualmedia://<ip>/redfish/v1/Systems/System.Embedded.1` — the
exact Dell form `app.domain.value_objects.bmc_address.parse_bmc_address`
documents, which round-trips into a Metal3 `BareMetalHost`'s
`spec.bmc.address`. OME's device summary does not expose the iDRAC's own
MAC on the validated path, so `bmc_mac` is left unset.

---

# Superseded: what OME's `InventoryDetails` taught us

**Everything below described the OME-only collector, whose hardware
mappers were deleted in
`docs/adr/0020-dell-identity-from-ome-hardware-from-redfish.md`.** Hardware
now comes from each server's iDRAC over Redfish, which reports measured
values and needs none of these heuristics.

It is kept, in full, for three reasons. Every fact here cost a live
appliance run to learn, and a fact without its provenance becomes folklore
nobody dares change. The OME field names are still the reference if anyone
ever needs `InventoryDetails` again — as a fallback, a cross-check, or for
a field Redfish does not carry. And the *reasons* these fields could not be
trusted are the entire justification for ADR-0020: delete them and the next
person re-derives the same wrong design.

Read it as history. None of the functions named below still exist.

## NIC status (superseded)

`serverNetworkInterfaces` nests interface -> `Ports` -> `Partitions`. The
MAC is per partition (`CurrentMacAddress`); link status and speed are per
port and shared by that port's partitions. `mapping.nics_from_interfaces`
produces one `ProviderNic` per partition — name (`Fqdd`, else `NicId`-`PortId`),
MAC, `speed_mbps`, `link_state` — which ingest turns into
`NetworkInfo.interfaces` (with `link_state` mapped onto
`app.domain.enums.LinkState`). `nic_macs` is then derived from those NICs
as the flat set identity correlation keys on.

**Two encodings here are unverified assumptions, flagged like the capacity
one.** The port's `LinkStatus` is mapped by
`_LINK_STATUS_TEXT_MAP`/`_LINK_STATUS_CODE_MAP`: string values
("Connected"/"Disconnected"/…) are reliable, but whether OME sends a string
or an integer here — and what integer codes mean — must be confirmed on
hardware; the numeric map (`1 -> UP`, `2 -> DOWN`) is a best guess and
anything else becomes `UNKNOWN`. `LinkSpeed` is assumed to be in **Mbps**;
confirm it is not bits/sec or an enum code.

This deliberately differs from the production scanner, which selected a
**single** PXE-boot MAC by server-name heuristics — H100/H200 names took
the third interface, "data"/`-<N>tb-` names the last interface's last
port's last partition, everything else the first. That selection was
specific to DHCP reservation and has no field on `ProviderServer`; the
knowledge is preserved here rather than in code because this platform wants
the full per-NIC view, not the one boot NIC. If a future need arises to
mark the primary NIC, this is the rule it followed in production.

## Capacity units (superseded)

**Memory** (`serverMemoryDevices` `Size`) is read as **megabytes**, carried
over from the production scanner: `memory_total_bytes = sum(Size) * 1024^2`.
Still an assumption (the analogue of the UCS `total_memory` MB assumption
`docs/adr/0009` flags) — confirm a known-32 GiB DIMM reports `Size == 32768`.

**Disks** are **decimal** (base-1000): a "480GB" drive is 480e9 bytes.
`_disk_capacity_bytes` reads the capacity from the Dell **model string
first** ("... M.2 480GB", "... U.2 1.92TB") because it is the one source
that carries its own unit. The numeric fields
(`Size`/`Capacity`/`CapacityBytes`/`SizeInBytes`/`RawSize`/`DiskSize`) are
only a fallback for a disk whose model has no size token.

This ordering is deliberate and was forced by live hardware: a
`serverArrayDisks` entry *does* populate a `Size` field, but in an
ambiguous unit — a 1.92 TB disk reported ~1.9e6 (i.e. MB, not bytes).
Read as bytes it collapses to `0.0 GB`, and because it was tried first it
shadowed the correct model-string value. Reading the unit-bearing model
string first sidesteps the unit ambiguity entirely. If a raw
`serverArrayDisks` entry ever confirms the true unit of a numeric field,
that field can become primary again.

The dry-run renders the per-server total in **TB** and each drive in GB
below 1 TB / TB at or above (`_format_tb`/`_format_disk_size`).

## CPU summary (superseded)

From `serverProcessors` `InventoryInfo`: `sockets` is the entry count,
`cores` sums `NumberOfCores`, and the model is the first entry's
`ModelName`, falling back to `BrandName` then `Family`.

**Threads** proved to be the fragile field on live hardware: none of the
expected fields were present and no HT flag was either, so the sum came out
`0`. `_logical_processors` tries several field names
(`NumberOfLogicalProcessors`, `LogicalProcessorCount`, `NumberOfThreads`,
`ThreadCount`); when none is present, threads falls back **unconditionally**
to `2 * cores`, since the Dell Xeons in this fleet run hyperthreaded.
**This is a heuristic, not observed** — it over-reports on any machine with
HT physically off. Paste one raw `serverProcessors` entry to pin the real
thread field and drop the fallback.

## Drive health (superseded)

`serverArrayDisks` entries report status either as a numeric OME rollup code
(`Status`: 1000 normal, 2000 unknown, 3000 warning, 4000 critical) or a
string (`PrimaryStatus`/`Status`). `mapping._drive_health` handles both.

**Media type** was `UNKNOWN` on live hardware — `MediaType` is not the
plain "SSD"/"HDD" string first assumed. `_media_type` now scans
`MediaType`, `BusType` and the model string together and classifies NVMe /
SSD / HDD from any of them (the fleet's drives name themselves "NVMe" in the
model, so they resolve to `NVME`). Still worth confirming the real
`MediaType` encoding from a raw entry.
