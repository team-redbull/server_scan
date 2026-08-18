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
- `GET /ProfileService/Profiles` — one entry per deployed server profile.
  `ProfileName` is the server's operator-facing name (and the platform's
  site/classification source); `TargetName` is the server's iDRAC IP.
- `GET /DeviceService/Devices` — one entry per managed device. `DeviceName`
  is the iDRAC IP (the join key back to a profile's `TargetName`), `Model`
  is the hardware model, `DeviceServiceTag` is the service tag used as the
  server serial, and `Id` is the device handle for inventory calls.
- `GET /DeviceService/Devices(<id>)/InventoryDetails('<section>')` — one
  hardware section for one device, returned in an `InventoryInfo` array.
  The collector reads `serverProcessors`, `serverMemoryDevices`,
  `serverStorage` and `serverNetworkInterfaces`. OME populates these from
  the server's iDRAC, so "collect from OpenManage and iDRAC" is one REST
  surface, not two connections.

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

`OpenManageProvider.list_servers` makes the two bulk calls once
(`/ProfileService/Profiles`, `/DeviceService/Devices`), joins each profile
to its device by iDRAC IP, then inventories the matching profiles a
bounded batch at a time (`INVENTORY_OME_INVENTORY_CONCURRENCY`). The
`name_pattern` prunes the expensive per-device inventory to this platform's
own fleet *before* any inventory call is spent — but it is only an
efficiency gate; the authoritative name filter is still
`tools.run_collector._NameFilteredProvider`, exactly as for Cisco.

A missing managed device, or an inventory section that fails to load,
degrades to empty rather than dropping the server: identity from the
profile is worth ingesting even when detail is partial. Any such gap is
recorded on `collection_errors`, which `tools.run_collector` reads to
report the run as **PARTIAL** (exit 3) rather than a silently-complete
success — the same honesty the Cisco collector applies to an unreachable
domain.

`external_id` is the service tag when the profile has a joined device, and
`ome-profile:<ProfileName>` otherwise, so it is always present for the
identity correlation ladder's per-manager step. Correlation itself keys on
`(vendor, serial)` (`app.application.services.ingest`), where serial is the
`DeviceServiceTag`.

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

## NIC status

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

## Capacity units

The production scanner treated OME's memory-module `Size` and physical-disk
`Size` as **megabytes** (it divided by 1024 to report GiB). This module
follows that: `memory_total_bytes` and each drive's `capacity_bytes` are
`Size * 1024 * 1024`. **This is an assumption, not a verified fact** — the
analogue of the UCS `total_memory` MB assumption that
`docs/adr/0009` flags as unsettled. Confirm it against a real appliance
(a known-32 GiB DIMM should report `Size == 32768`) and correct here if OME
actually reports bytes.

## CPU summary

From `serverProcessors` `InventoryInfo`: `sockets` is the entry count,
`cores` and `threads` sum `NumberOfCores` / `NumberOfLogicalProcessors`
across entries, and the model is the first entry's `ModelName`, falling
back to `BrandName` then `Family`. The scanner stored only a single
processor's core count; summing across sockets is the correction made
here, defensible as the total-core semantics `ProviderServer.cpu_cores`
expects.

## Drive health

`serverStorage` entries report status either as a numeric OME rollup code
(`Status`: 1000 normal, 2000 unknown, 3000 warning, 4000 critical) or a
string (`PrimaryStatus`/`Status`). `mapping._drive_health` handles both and
maps onto the platform's `HealthSeverity`. The scanner did not collect
drive health at all, so this mapping is **inferred from OME's documented
rollup codes, not observed** — verify against a real appliance, especially
whether `Status` arrives as an int or a string.
