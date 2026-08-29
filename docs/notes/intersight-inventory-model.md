# Intersight inventory model — research notes

Researched against the installed Intersight Python SDK wheel, version
`1.0.11.2026072720`, extracted at
`/tmp/claude-1000/-home-tomer-code-server-scan/4bc3cc6e-a17e-49ef-8dbb-90dd7ce00f2d/scratchpad/isdk/ext`
(`intersight/model/*.py`, `intersight/api/*.py`). All citations below are
`file path:line range` into that extracted wheel unless marked otherwise.
Web sources (developer.cisco.com) were not fetched this pass — the
installed SDK's generated `openapi_types` dicts and docstrings are a
direct dump of the OpenAPI contract and were sufficient and authoritative
per the researcher's own tie-break rule (contract/installed source wins
over prose docs). Nothing here was cross-checked against a live
Intersight tenant — several items are marked `UNVERIFIED` for that
reason and say what a live response would settle.

## Summary

`compute.PhysicalSummary` (and its two concrete siblings `compute.Blade`
/ `compute.RackUnit`) supply the whole-server identity/CPU/memory
snapshot in one MO family — cheap at scale. Memory is `int` and
documented "in MB" on the summary; a separate per-DIMM `capacity` field
is a **string** documented "in MiB" on `memory.Unit`, which is the one
authoritative-in-writing unit statement in this SDK (see §2 — treat the
server-level field as MiB pending live confirmation, not MB literally).
NICs split cleanly into physical (`adapter.ExtEthInterface`, "External
Ethernet Interface") vs. host-facing virtual (`adapter.HostEthInterface`,
explicitly documented as carrying "vNIC" operational state) — this maps
directly onto `ProviderAttachment.interface_kind`. GPUs
(`graphics.Card`/`graphics.Controller`/`pci.Device`) carry only identity
fields (model/vendor/serial/PCI address) — **no memory, temperature,
power or ECC field exists anywhere in this SDK version**, unlike Redfish.
**The most consequential finding is #7 below: `compute.PhysicalSummary
.Name` is documented, in writing, to never be an operator-assigned
hostname** — it's the FI cluster name + chassis/slot, the CIMC's own
name, or "model and chassis/server Id", depending on management mode.
Using it as `ProviderServer.name` would silently defeat site parsing and
`^ocp` filtering exactly the way `computeBlade.name` did for UCS
Manager — the fix is `server.Profile.name`, reached via a relationship,
not a peer field on the same MO, for two of the three management modes.

## 1. `compute.PhysicalSummary` — full attribute list and comparison

Confirmed to exist; full `openapi_types` dict at
`intersight/model/compute_physical_summary.py:175-254`, `attribute_map`
(pythonic name -> wire name) at `:264-343`.

Attributes asked about, with type and wire name:

| Attribute (python / wire) | Type | Present? |
|---|---|---|
| `name` / `Name` | `str` | yes |
| `model` / `Model` | `str` | yes |
| `serial` / `Serial` | `str` | yes |
| `moid` / `Moid` | `str` | yes (inherited `MoBaseMo` field) |
| `dn` / `Dn` | `str` | yes |
| `total_memory` / `TotalMemory` | `int` | yes |
| `num_cpus` / `NumCpus` | `int` | yes |
| `num_cpu_cores` / `NumCpuCores` | `int` | yes |
| `num_threads` / `NumThreads` | `int` | yes |
| `mgmt_ip_address` / `MgmtIpAddress` | `str` | yes |
| `alarm_summary` / `AlarmSummary` | `ComputeAlarmSummary` | yes |
| `management_mode` / `ManagementMode` | `str` | yes |
| `chassis_id` / `ChassisId` | `str` | yes |
| `ancestors` / `Ancestors` | `[MoBaseMoRelationship]` (nullable) | yes |
| `vendor` / `Vendor` | `str` | yes |
| `uuid` / `Uuid` | `str` | yes |

All 15 asked-about fields exist directly on `compute.PhysicalSummary`.
It is a broad, HATEOAS-flattened "summary" MO — Cisco's own compute
inventory read model, not a thin projection — carrying CPU/memory
counts, `AlarmSummary`, KVM IP addresses, `hardware_uuid`, `asset_tag`,
`user_label`, `platform_type`, `personality`, `scaled_mode`, `firmware`,
`connection_status`, and both `equipment_chassis` and
`registered_device` relationships
(`intersight/model/compute_physical_summary.py:175-254`).

**`compute.Blade` vs. the summary** — full `openapi_types` at
`intersight/model/compute_blade.py` (grepped, not paginated inline
above): it carries every field the summary does (same
`total_memory`/`available_memory`/`num_cpus`/etc. block,
`compute_blade.py:658-693` for descriptions) **plus** typed relationship
collections the summary doesn't expose: `adapters`
(`[AdapterUnitRelationship]`), `processors`
(`[ProcessorUnitRelationship]`), `memory_arrays`
(`[MemoryArrayRelationship]`), `storage_controllers`
(`[StorageControllerRelationship]`), `graphics_cards`
(`[GraphicsCardRelationship]`), `pci_devices`
(`[PciDeviceRelationship]`), and a direct `bmc`
(`ManagementControllerRelationship`) — the summary has no `bmc` field at
all, only `mgmt_ip_address`/`ipv4_address` as bare strings. `compute
.RackUnit` is the same shape with rack-specific relationships
(`psus`, `fanmodules`, `rack_enclosure_slot`,
`sas_expanders`) instead of `equipment_chassis`
(`intersight/model/compute_rack_unit.py`, same field block).

**Practical implication**: `compute.PhysicalSummary` is enough for the
identity/CPU/memory/BMC-IP fields, but the typed relationship lists
(needed for CPU model via `processors`, memory ECC type via
`memory_arrays`, NICs via `adapters`, storage via `storage_controllers`,
and the BMC's own MAC via `bmc`) exist only on `compute.Blade` /
`compute.RackUnit`, not on the summary — a real collector will need
per-server queries against whichever of the two concrete classes
applies (`PlatformType` distinguishes them,
`compute_physical_summary.py:220`, description not separately captured
this pass — `UNVERIFIED`, check `platform_type`'s enum values against a
live response or the API reference before branching on it).

## 2. Units — the critical question

**`compute.PhysicalSummary.total_memory`**: `int`,
`intersight/model/compute_physical_summary.py:231`. Its own docstring is
unhelpfully bare: `"The total memory available on the server.."`
(`:500`, repeated `:694`) — **no unit stated**. Same on `compute.Blade`
(`:693`) and `compute.RackUnit` (`:727`, `:960`) — identical wording,
still no unit.

**`compute.PhysicalSummary.available_memory`** (sibling `int` field,
`:181`) **is** documented with a unit, on the summary specifically:
`"Total memeory of the server in MB.."` (`:450`, `:644` — `memeory` sic,
a typo in the vendor's own OpenAPI description). `compute.Blade`
/`compute.RackUnit`'s `available_memory` docstring drops the unit again:
`"The amount of memory available on the server.."`
(`compute_blade.py:658`, `compute_rack_unit.py:692`).

**`memory.Unit.capacity`** (per-DIMM, `str` not `int` —
`intersight/model/memory_unit.py:327`) is the one field in this whole
SDK with an unambiguous, twice-repeated unit statement: `"This
represents the memory capacity in MiB of the memory unit on a
server.."` (`:519`, `:686`).

**Conclusion, with confidence level**:
- Per-DIMM capacity is **MiB**, stated as fact by the vendor
  (`memory_unit.py:519`).
- `available_memory` on `compute.PhysicalSummary` is stated as **MB**
  (`compute_physical_summary.py:450`) — but note the vendor's own
  wording is casual ("memeory... in MB") next to a per-DIMM field that
  says MiB explicitly. Cisco's Redfish/UCS docs are not internally
  rigorous about MB-vs-MiB in general (this repo's own UCS Manager
  mapping assumes UCS's `storageLocalDisk.size` is MB by convention, not
  by a documented guarantee — `ucs_manager/mapping.py:1-16`).
- `total_memory` — the field this repo actually needs — **has no unit
  documented anywhere in the SDK for any of the three classes that carry
  it.** Given it's a sibling `int` field of `available_memory` on the
  same three MOs, sharing a plausible common origin (a DIMM-capacity
  sum, which the SDK states in MiB), the highest-confidence assumption
  is **MiB**, but this is **UNVERIFIED** and the SDK text alone cannot
  settle it — `available_memory`'s docstring literally says "MB" one
  field away. **What would settle it**: one live
  `compute.PhysicalSummary` (or `compute.Blade`) response from a real or
  emulated Intersight tenant for a server of known installed RAM,
  comparing `TotalMemory` against `sum(memory.Unit.Capacity)` for that
  server's DIMMs — if they match, `TotalMemory` is MiB; if `TotalMemory`
  is ~4.86% larger than the MiB sum, it's actually MB read as MiB, or
  vice versa. Do not ship a hardcoded MB assumption for `total_memory`
  without this check — the two candidate units differ by that ~4.86% at
  typical DIMM sizes, small enough to not obviously "look wrong" in a
  spot check but large enough to misreport a health/capacity policy at
  scale.

**Storage capacity**: `storage.PhysicalDisk.size` and `.raw_size` are
both `str`, both explicitly documented **"in MB"**:
`"The size of the physical disk in MB.."`
(`intersight/model/storage_physical_disk.py:634`) and `"The raw size of
the physical disk in MB.."` (`:630`). This one is settled by the SDK
text itself, no live check needed. Note `size`/`raw_size` are strings
(not `int`) — same `str`-typed-numeric pattern as `memory.Unit.capacity`
— a collector must parse them. `non_coerced_size_bytes` (`:611`) is a
true `int` in bytes and may be a better source than parsing the MB
string, if it's reliably populated — `UNVERIFIED`, no docstring
distinguishes when it's set vs. `size`/`raw_size`.

## 3. NICs and attachments — PHYSICAL vs. VNIC, confirmed by docstring text

**`adapter.ExtEthInterface`** = the physical cabled uplink. Docstring
text confirms this in the vendor's own words: `"MAC address of an
External Ethernet Interface.."` (`adapter_ext_eth_interface.py:288`),
`"Admin configured state of an External Ethernet Interface.."` (`:284`),
`"DN of peer end-point attached to an External Ethernet Interface.."`
(`:292`), `"Operational state of an Interface.."` (`:315`). Full
attribute list: `class_id, object_type, admin_state, ep_dn,
ext_eth_interface_id, interface_type, mac_address, network_type,
oper_reason, peer_aggr_port_id, peer_dn, peer_port_id, peer_slot_id,
switch_id, oper_state` plus base relationships (`adapter_unit`,
`inventory_device_info`, `registered_device`) and `acknowledged_peer_
interface`/`peer_interface` (both `EtherPhysicalPortBaseRelationship`)
(`adapter_ext_eth_interface.py`, grepped `openapi_types`). No speed
field.

**`adapter.HostEthInterface`** = the OS-facing vNIC. The SDK's own
docstring says so explicitly, twice, using the word "vNIC": `"The
operational state of the Active vNIC. vNIC operational state
information is updated by events from the adapter... For Intersight
Managed Domains Mode domains (IMM), the vNIC's peer object Vethernet
will have the current operational state..."`
(`adapter_host_eth_interface.py:517`, `:683`). Field name itself:
`host_eth_interface_id (int): Unique Identifier for an Host Ethernet
Interface within the adapter object.` (`:521`). Full attribute list
includes `mac_address, admin_state, active_oper_state,
active_veth_oper_state, standby_oper_state, standby_veth_oper_state,
standby_vif_id, vif_id, peer_dn, vnic_dn, pin_group_name,
virtualization_preference, qinq_enabled, qinq_vlan` plus relationships
`adapter_unit`, `vethernet`/`standby_vethernet`
(`NetworkVethernetRelationship`), `pinned_interface`. No speed field
either.

This is a clean, self-documenting match to this repo's split:
`interface_kind="PHYSICAL"` for `adapter.ExtEthInterface`,
`interface_kind="VNIC"` for `adapter.HostEthInterface` — exactly the
UCS Manager `adaptorExtEthIf`/`adaptorHostEthIf` split this repo already
relies on (`ucs_manager/mapping.py:_nic_macs`, `:_attachments`), which
makes sense since `ucscsdk`/`ucsmsdk` and this Intersight schema
describe overlapping Cisco hardware concepts.

**Relating an interface back to its server**: both interface types
carry an `adapter_unit` relationship (`AdapterUnitRelationship`) — not a
direct link to the compute unit. `adapter.Unit` itself
(`adapter_unit.py`) carries `compute_blade`
(`ComputeBladeRelationship`) and `compute_rack_unit`
(`ComputeRackUnitRelationship`) relationships, and in the other
direction owns `ext_eth_ifs`/`host_eth_ifs`/`host_fc_ifs`/
`host_iscsi_ifs` (each `[XRelationship]`) — i.e. the exact list needed
per server. **Chain**: `compute.Blade`/`compute.RackUnit` -> (query
`adapter.Unit` where `ComputeBlade`/`ComputeRackUnit` relationship ==
this server's Moid) -> `adapter.Unit.ext_eth_ifs` /
`.host_eth_ifs` -> fetch each `adapter.ExtEthInterface` /
`adapter.HostEthInterface`. `compute.Blade`/`compute.RackUnit` also
expose an `adapters` relationship list directly
(`compute_blade.py` openapi_types, `'adapters':
([AdapterUnitRelationship], none_type,)`), which is the more direct
starting point than querying `adapter.Unit` independently.

**Fabric/switch side** (needed for `fabric`/`fabric_model`/
`fabric_serial`, mirroring what `ucs_manager/mapping.py:_attachments`
does with `networkElement`): `ether.PhysicalPort`
(`ether_physical_port.py`) and `ether.HostPort` (`ether_host_port.py`)
are both **switch/IOM-side** port MOs, not server-side — `ether
.PhysicalPort` sits under a `port_group`/`port_sub_group` on a Fabric
Interconnect-class device (has `switch_id`, `slot_id`, `port_id`,
`role`, `mac_address`, `oper_speed`/`admin_speed` as `str`, and
`peer_dn`/`peer_interface` to the far end); `ether.HostPort` is the same
shape but scoped to an IOM (`equipment_io_card_base`/
`equipment_switch_card` relationship instead of `port_group`). Neither
one is a server-facing NIC — they are the network element's own ports;
`ProviderAttachment.fabric_port` should resolve through `adapter
.ExtEthInterface.peer_dn` to whichever of these two the DN identifies,
same pattern as UCS Manager's `adaptorExtEthIf.peer_dn`
(`ucs_manager/mapping.py:_attachments`, `fabric_port=getattr(mo,
"peer_dn", None)`). Neither `ether.PhysicalPort` nor
`ether.HostPort` carries a numeric `speed_mbps` — both expose
`oper_speed`/`admin_speed` as free-form strings (`"Current Operational
speed for this port.."`, `ether_physical_port.py:374`) — parsing that
string (likely `"10Gbps"`/`"25Gbps"` style, **UNVERIFIED**, no enum
listed) is the only path to `ProviderAttachment.speed_mbps`; there is no
integer Mbps field anywhere in this NIC chain.

**`vnic.EthIf`** (`vnic_eth_if.py`) is a **policy/design-time** object,
not a live-inventory one — it's what a Server Profile's LAN
Connectivity Policy declares (fields: `placement`, `order`,
`eth_adapter_policy`, `eth_network_policy`, `mac_pool`, `profile`
relationship, `sp_vnics`/`lcp_vnic` relationships back to the owning
profile). It has no `admin_state`/`oper_state` inventory fields at all
— it's config, not observed state. **Do not use it for
`ProviderAttachment`** — it answers "what was configured", not "what is
plugged in and up"; `adapter.HostEthInterface` is the deployed/observed
counterpart and is what this repo's `ProviderAttachment.oper_state`
needs.

## 4. BMC address and MAC

**Bare address**: `compute.PhysicalSummary.mgmt_ip_address` — `"Management
address of the server.."` (`compute_physical_summary.py:473`) — and a
second field `ipv4_address` — `"The IPv4 address configured on the
management interface of the Integrated Management Controller.."`
(`:463`). Both are plain strings (no scheme/URI prefix implied by the
description); which one is populated in which mode is **UNVERIFIED** —
`mgmt_ip_address` reads as the general field, `ipv4_address` as an
IMC-specific one, but the SDK gives no rule for precedence. A live
response comparing both fields for the same server would settle it.
Neither field carries a MAC.

**MAC and richer detail**: `management.Interface`
(`management_interface.py`) is the authoritative source for the BMC's
own MAC — `"MAC address configured for the interface.."` (`:270`) —
plus `ip_address`/`ipv4_address`/`ipv4_gateway`/`ipv4_mask`/`gateway`/
`switch_id`/`host_name`. It relates to `management.Controller` via a
`management_controller` relationship
(`ManagementControllerRelationship`, `management_interface.py`
openapi_types). `management.Controller`
(`management_controller.py`) is the BMC itself as a managed component —
carries `compute_blade`/`compute_rack_unit` relationships (the reverse
direction of `compute.Blade.bmc`/`compute.RackUnit.bmc`,
`compute_blade.py`: `'bmc': (ManagementControllerRelationship,)`) and
`management_interfaces` (`[ManagementInterfaceRelationship]`, plural —
a BMC can have more than one management interface). `management
.Controller.model` exists but describes the **server's** model, not the
BMC chip: `"Model of the endpoint that houses the management
controller.."` (`management_controller.py:291`).

**Recommended chain, most-specific first**: `compute.Blade`/
`compute.RackUnit.bmc` -> `management.Controller.management_interfaces`
-> `management.Interface.mac_address`/`.ip_address`, falling back to
`compute.PhysicalSummary.mgmt_ip_address` when only the summary MO was
queried (e.g. for a cheap first pass across the whole fleet before
per-server detail calls) — same "authoritative first, then a documented
fallback" pattern this repo already uses for UCS Manager's BMC address
(`ucs_manager/mapping.py:_management_ip_addr`,
`:_bmc_address`).

## 5. Storage

**`storage.PhysicalDisk`** (`storage_physical_disk.py`) per-drive
attributes: `disk_id` (`str`, identity), `pid`/`part_number` (model),
`serial` inherited from `EquipmentBase` (same block as `vendor`/
`model`/`revision`/`presence` seen on every equipment MO in this SDK —
`storage_physical_disk.py` openapi_types), `type` (`str`, media type —
enum values not captured this pass, **UNVERIFIED**), `size`/`raw_size`
(capacity, `str`, "in MB" — settled, §2), `health` (`str`) plus
`health_message`/`health_resolution`, `disk_state`/`drive_state`
(`str`), `failure_predicted` (`bool`), `predictive_failure_count`
(`int`), `media_error_count`/`read_io_error_count`/
`write_io_error_count` (`int`), `power_on_hours`/`power_cycle_count`
(`int`), `percent_life_left`/`predicted_media_life_left_percent`
(`int`, SSD wear). Relates to its owner via `storage_controller`
(`StorageControllerRelationship`).

**`storage.Controller`** (`storage_controller.py`): `controller_id`
(`str`), `controller_status`/`oper_state` (`str`), `raid_support`
(`str`), `total_cache_size` (`int` — **cache** size, not a disk-capacity
rollup), `memory_correctable_errors`/`ecc_bucket_leak_rate` (`int` —
these are the **controller's own onboard memory's** ECC counters, not a
system-memory or GPU-memory ECC signal), plus base equipment fields
(`model`/`vendor`/`serial`/`presence`). Owns `physical_disks`
(`[StoragePhysicalDiskRelationship]`) and `disk_group`/`disk_slot`
lists. Relates to its server via `compute_blade`/`compute_board`/
`compute_rack_unit` relationships.

**Per-server storage total**: no rollup field exists anywhere in this
chain — no "TotalCapacity" on `storage.Controller` or on
`compute.PhysicalSummary`/`Blade`/`RackUnit`. A collector must sum
`storage.PhysicalDisk.size` (parsed MB string) across every disk under
every controller reachable from `compute.Blade`/`RackUnit
.storage_controllers` -> `storage.Controller.physical_disks` — the same
compute-then-sum approach this repo's UCS Manager provider already uses
over `storageLocalDisk` (`ucs_manager/mapping.py:_storage_drives`), and
for the same reason: `storage_total_bytes` per `ProviderServer`'s own
docstring must be a real sum, not an assumed zero when a drive's size
couldn't be parsed (`provider.py:116-117` `storage_drives`/
`storage_total_bytes`, mirroring the `nic_macs`/`gpus` "`None` vs. empty
tuple" contract at `provider.py:70-79`).

## 6. GPUs — identity only, no telemetry fields exist in this SDK

`pci.Device` (`pci_device.py`): `device_id`, `firmware_version`, `pid`,
`slot_id`, plus a `graphics_cards` relationship
(`[GraphicsCardRelationship]`) and `compute_blade`/`compute_rack_unit`
relationships back to the server. No memory/temp/power/ECC.

`graphics.Card` (`graphics_card.py`): `card_id` (`int`), `gpu_id`
(`str`) — `"The identifier of the graphics processor unit.."` (`:515`)
— `num_gpus` (`str`, despite the name a count of "controllers under
each card", `:518`), `mode` (`str` — `"The current mode of the graphics
card.."`, `:517`, likely a compute/graphics-mode toggle, not power
state), `device_id`/`vendor_id`/`sub_device_id`/`sub_vendor_id` (`int`
PCI IDs), `pci_address`/`pci_address_list`/`pci_slot`/`part_number`/
`pid`, plus base equipment fields `model`/`vendor`/`serial`/`revision`/
`presence`. Relates to `compute_blade`/`compute_board`/
`compute_rack_unit` and owns `graphics_controllers`
(`[GraphicsControllerRelationship]`).

`graphics.Controller` (`graphics_controller.py`): `controller_id`
(`int`), `pci_addr`, `pci_slot`, plus the same base equipment fields
(`model`/`vendor`/`serial`/`presence`/`revision`) and a
`graphics_card` relationship back up.

**Grepped every model file for GPU-adjacent memory/temperature/power/
ECC text** (`grep -ril "gpu.*memory|gpu.*temperature|gpu.*power|gpu.*
ecc" intersight/model/`) — the only hits are `hci_*_gpu*.py` (HyperFlex
virtual/physical GPU passthrough assignment objects — about VM GPU
*allocation*, not hardware telemetry) and none of them carry those
fields either. **Conclusion: this SDK version has no field for GPU
memory size, memory type, ECC status, error counts, temperature, or
power draw, on any of `pci.Device`, `graphics.Card`, or `graphics
.Controller`.** This is a real capability gap versus this repo's
existing Redfish-based GPU fields (`provider.py:119-125`'s `gpus`
docstring references `app.domain.models.hardware.Gpu` — an Intersight
collector can populate `model`/`vendor`/`serial`/PCI identity only, and
must report `memory_bytes`/`temperature`/`power`/`ecc`/error counts as
`None` (not `0`) for every GPU, per the DTO's own "`None` means
'could not read'" contract (`provider.py:70-79`). This is not a
collector bug to fix later — it's a genuine ceiling of what this MO
family exposes. **What would settle whether a newer Intersight API
version adds these fields**: check the current OpenAPI spec at
`https://intersight.com/apidocs/apirefs/` (unreached this pass) or a
newer SDK wheel version against this one.

## 7. THE NAME TRAP — settled, in the vendor's own words

`compute.PhysicalSummary.name` docstring, verbatim (identical on
`compute.Blade`/`compute.RackUnit`, same field inherited):

> "The name of the UCS Fabric Interconnect cluster or Cisco Integrated
> Management Controller (CIMC). When this server is attached to a UCS
> Fabric Interconnect, the value of this property is the name of the UCS
> Fabric Interconnect along with chassis/server Id. When this server
> configured in standalone mode, the value of this property is the name
> of the Cisco Integrated Management Controller. when this server is
> configired in IMM mode, the value of this property contains model and
> chassis/server Id."
> — `compute_physical_summary.py:475` (typo "configired" is the
> vendor's, reproduced verbatim), repeated `:669`.

So, **by management mode** (enum values from
`compute_physical_summary.py:471`, quoted in full in §9 below):

- **`UCSM`** (server attached to a UCS Fabric Interconnect managed
  through Intersight): `Name` = FI cluster name + chassis/server Id —
  not an operator hostname, exactly analogous to `computeBlade.name`
  being empty/useless on raw UCS Manager.
- **`IntersightStandalone`**: `Name` = the CIMC's own configured name —
  closer to useful (an operator likely did set the CIMC hostname) but
  still not guaranteed to be the `ocp4-...` convention this platform
  parses sites/patterns from.
- **`Intersight`** (IMM): `Name` = "model and chassis/server Id" —
  guaranteed **not** a hostname (literally says "model", e.g. something
  shaped like `UCSC-C240-M6SX-chassis-1-server-1`, not a token the site
  parser or `^ocp` filter can match).

**In no mode is `compute.PhysicalSummary.Name` a safe source for
`ProviderServer.name`.** Using it directly would reproduce, and in the
IMM case worsen, the exact defect ADR-0009 records for raw
`computeBlade.name` (`CLAUDE.md`'s collector-architecture section, "A
UCS server's name comes from its **service profile**, not
`computeBlade.name`, which is empty in practice").

**The real name, and the relationship field that reaches it**:
`server.Profile.name` — `"Name of the profile instance or profile
template.."` (`server_profile.py:515`) — reached via the *inverse*
relationship on the profile, not a forward field on the compute MO:
`server.Profile.associated_server` (type `ComputePhysicalRelationship`,
`server_profile.py:492`) and `.assigned_server` (`:491`) both point
*from* the profile *to* the compute MO. `ComputePhysicalRelationship`
itself (`compute_physical_relationship.py`) carries `moid`, `dn`,
`selector`, `link` plus (per this SDK's flattened relationship style)
the full set of `compute.Blade` fields inline — meaning a profile
lookup alone may already carry enough of the compute MO's identity to
correlate, but the safe correlation key is `associated_server.moid ==
compute.PhysicalSummary.moid`. **`assigned_server` vs.
`associated_server` distinction is UNVERIFIED from docstrings alone**
(neither has its own explanatory text beyond `"[optional]"`,
`server_profile.py:491-492`) — `server_assignment_mode`'s docstring
(`:480`) distinguishes *Static* (server attached directly) from *Pool*
(assigned from a resource pool) as the two ways a server becomes
attached, which suggests `assigned_server` may be the Static-mode target
and `associated_server` the currently-deployed one regardless of how it
got assigned — treat `associated_server` as primary (it's the more
generically-named of the two and is what "this profile is running on
this box right now" should mean) but confirm against one live paired
`server.Profile`/`compute.PhysicalSummary` response before depending on
it, and prefer whichever one is non-null when they disagree.

**A real, SDK-internal disagreement worth flagging** (rule #2 — record
disagreements explicitly): `compute.PhysicalSummary.management_mode`'s
docstring enumerates **three** values — `IntersightStandalone`, `UCSM`,
`Intersight` (`compute_physical_summary.py:471`, quoted §9) — but
`server.Profile.management_mode`'s docstring enumerates only **two** —
`IntersightStandalone`, `Intersight` (`server_profile.py:529`, no
`UCSM`). Taken at face value, this means **`UCSM`-mode servers may not
get a `server.Profile` MO in Intersight at all** — Central/UCSM-domain
service profiles might not be projected into Intersight's `server
.Profile` collection the way IMM profiles are. If so, the `UCSM`-mode
name source has to fall back to `compute.PhysicalSummary
.service_profile` (§ next paragraph) rather than a `server.Profile`
join. **This is the single highest-value thing to verify before writing
the collector**: query `server.Profile` with `$filter=ManagementMode eq
'UCSM'` against a live tenant with UCSM domains registered — an empty
result confirms the gap and confirms the DN-parsing fallback is
required, not optional.

**`service_profile` (`str`, a DN) exists directly on
`compute.PhysicalSummary`/`Blade`/`RackUnit`** —
`"The distinguished name of the service profile to which the server is
associated to. It is applicable only for servers which are managed via
UCSM.."` (`compute_physical_summary.py:496`, identical wording on
`compute_blade.py`/`compute_rack_unit.py`). This is the fallback for
`UCSM`-mode if the SDK disagreement above is confirmed real: the DN
itself is shaped like UCS Manager's own service-profile DN
(`org-root/org_tlv/ls-worker-01`), and this repo already knows how to
pull a name and an org-derived site fallback out of that exact shape
(`ucs_manager/mapping.py:_server_name`, and the org-DN site fallback
documented in `CLAUDE.md`'s "site is parsed from its name" section) —
but the DN's last segment is not guaranteed to *be* the profile's
`Name` property (UCS DNs use a `ls-<name>` naming convention by
UCSM/UCS Central's own construction, not by any Intersight guarantee)
— **UNVERIFIED without a live `UCSM`-mode server's `service_profile`
DN value to inspect.**

## 8. Server Profile Template

`server.ProfileTemplate` (`server_profile_template.py`) — confirmed to
exist as its own MO, not a variant flag on `server.Profile`. Shares the
same base "profile" fields (`name`, `description`, `type`,
`target_platform`, `server_family`, `management_mode`, policy-bucket
fields) via a common `type` discriminator: `server.Profile.type` is
`"instance"` by default (`server_profile.py:516`, `"Defines the type of
the profile. Accepted values are instance or template..."`), so
`server.ProfileTemplate` is the `"template"` variant of essentially the
same schema (both files share nearly identical `openapi_types` blocks —
diffed by inspection, `server_profile.py` vs.
`server_profile_template.py`).

**Instance -> template link**: `server.Profile.src_template`
(`PolicyAbstractProfileRelationship`, `server_profile.py` openapi_types)
— this is the field to resolve `ProviderServer.profile_template_name`
/`profile_template_external_id` from: the relationship's target `Moid`
is the external id, and a separate `server.ProfileTemplate` lookup by
that Moid gives `.name`. This mirrors UCS Manager's own
`src_templ_name`/`oper_src_templ_name` pair
(`ucs_manager/mapping.py:_profile_template_fields`) closely enough that
the same two-value "name, then a resolvable external id, else fall back
to the name itself" shape this repo already uses should transfer
directly.

## 9. `ManagementMode` — full enum and per-mode field population

From `compute.PhysicalSummary`'s own docstring
(`compute_physical_summary.py:471`, verbatim):

> "The management mode of the server. * `IntersightStandalone` -
> Intersight Standalone mode of operation. * `UCSM` - Unified Computing
> System Manager mode of operation. * `Intersight` - Intersight managed
> mode of operation.. defaults to `IntersightStandalone`"

Three values, confirmed by the SDK's own generated docstring — no other
value is declared for this field anywhere in the file. (Contrast
`server.Profile.management_mode`'s two-value enum, §7 above — a real
discrepancy, not a duplicate citation.)

Field-population-by-mode is **not spelled out anywhere in this SDK** as
a table — everything below is inferred from the individual field
docstrings already cited, not stated as a single fact:

- **`UCSM`**: `service_profile` (DN string) populated on the compute MO
  itself (§7); `Name` = FI cluster + chassis/server id (§7); whether a
  `server.Profile` MO also exists is the open disagreement in §7.
- **`IntersightStandalone`**: `Name` = CIMC's own name; `server.Profile`
  exists (per its 2-value enum including this mode) and should carry
  the real assigned name via `associated_server`.
- **`Intersight`** (IMM): `Name` = "model and chassis/server Id";
  `server.Profile` exists (per its 2-value enum) — same
  `associated_server` join as standalone.

**Does a `UCSM`-mode server (also managed by UCS Central) appear in
Intersight at all?** The existence of the `UCSM` enum value on
`compute.PhysicalSummary.ManagementMode`, plus the UCSM-specific
`service_profile` field, says yes — Intersight can and does inventory
UCSM-domain servers when that domain is claimed/registered into
Intersight (a distinct registration from being registered with UCS
Central — **UNVERIFIED whether the two registrations are the same
event or independent**, this SDK has no field that names UCS Central as
a concept at all, `grep -ril "ucs.*central" intersight/model/` returned
nothing). This matters directly for this repo: **if a domain can be
double-registered (once with UCS Central, once with Intersight), an
Intersight collector and the existing `UCS_CENTRAL` collector could
both ingest the same physical servers** — correlation would fall to
`IngestService`'s existing `(vendor, serial_normalized)` key
(`CLAUDE.md`'s "Vendor is dell/cisco/hp/standalone" section) to merge
them into one document, same as any other overlapping-collector case,
but this should be confirmed operationally (which domains get
registered where, in practice) before both collectors are run against
the same fleet.

## 10. Vendor/manufacturer and model

`compute.PhysicalSummary.vendor` (`str`) — `"This field identifies the
vendor of the given component.."` (`compute_physical_summary.py:504`) —
generic wording, not hardcoded to "Cisco" in the docstring, but given
Intersight is a Cisco-only management plane for Cisco-owned/claimed
hardware, a real value is expected to always be a Cisco string (e.g.
`"Cisco Systems Inc"`) — **UNVERIFIED exact string** without a live
response; this repo's `Vendor` enum only needs to map it to `"cisco"`
regardless of the exact vendor string's punctuation, so exact-string
verification matters less here than it would for a field being parsed
structurally.

`model` (`str`) — `"This field identifies the model of the given
component.."` (`:474`) — example shape given nowhere in the SDK text
itself, but consistent with UCS's own model strings (e.g.
`UCSC-C240-M6SX`) based on this repo's own UCS Manager experience
(`ucs_manager/mapping.py` assumes the same convention for its `model`
field, unlabeled float value, no separate research needed here — same
vendor, same naming scheme).

## Open questions / UNVERIFIED

1. **`total_memory` unit (MB vs. MiB)** — the single highest-priority
   unknown; see §2. Settle with one live paired
   `compute.PhysicalSummary.TotalMemory` vs. `sum(memory.Unit.Capacity)`
   comparison for a server of known RAM size.
2. **Does `service_profile`'s DN, on a `UCSM`-mode compute MO, actually
   end in the profile's real `Name`** (the `ls-<name>` convention), or
   can it diverge? Settle with one live `UCSM`-mode server's
   `service_profile` value compared against that same profile's
   `server.Profile.Name` (if the SDK disagreement in §7 turns out to be
   wrong and `server.Profile` MOs do exist for `UCSM` mode after all).
3. **Does `server.Profile` exist at all for `UCSM`-mode servers?** The
   two-vs-three-value `ManagementMode` enum disagreement between
   `compute.PhysicalSummary` and `server.Profile` (§7) suggests no.
   Settle with a live `$filter=ManagementMode eq 'UCSM'` query against
   `server.Profile` on a tenant with UCSM domains registered.
4. **`assigned_server` vs. `associated_server` on `server.Profile`** —
   which one to prefer when they disagree (§7). Settle with one live
   profile that has gone through a reassignment (attached, detached,
   reattached to a different server) to see which field tracks "current"
   state.
5. **`mgmt_ip_address` vs. `ipv4_address` on `compute.PhysicalSummary`**
   — which is populated, in which mode, and whether they can disagree
   (§4). Settle with one live response per management mode.
6. **Do `UCSM`-mode servers reachable via Intersight overlap with the
   same servers reachable via this repo's existing `UCS_CENTRAL`
   collector** (§9) — an operational question about how a given fleet's
   domains get registered, not something the SDK can answer. Settle by
   asking the user/operator, or by comparing serials from both
   collectors' dry-run output against a shared test tenant if one
   becomes available.
7. **`platform_type` enum values** on `compute.PhysicalSummary`
   (`compute_physical_summary.py:220`) — not captured this pass, needed
   to decide when to fall back from the summary MO to `compute.Blade`
   vs. `compute.RackUnit` for the richer per-server queries (§1).
   Settle by grepping its docstring block directly
   (`compute_physical_summary.py`'s `_from_openapi_data`/`__init__`
   Keyword Args sections, same pattern used throughout this file).
8. **`storage.PhysicalDisk.non_coerced_size_bytes`** (`int`, true bytes)
   vs. parsing `size`/`raw_size` (MB strings) — which is more reliably
   populated. Settle with one live disk response.
9. **GPU field gap** (§6) — whether a newer Intersight API version (this
   SDK is `1.0.11.2026072720`) has since added GPU
   memory/temperature/power/ECC fields. Settle by checking
   `https://intersight.com/apidocs/apirefs/` directly or diffing against
   a newer SDK wheel.
10. **`graphics.Card.mode`'s actual values** and whether `num_gpus`
    (typed `str` despite being a count) is reliably numeric — neither
    docstring gives an enum or format. Settle with one live response.
