# ADR-0019: Dell collection splits — identity from OME, hardware from Redfish

- Status: Accepted
- Date: 2026-08-31
- Supersedes the collection half of the original Dell collector
  (`docs/dell-collectors.md`, "Collection flow")

## Context

The first Dell collector read everything from one OpenManage Enterprise
appliance: two bulk REST calls to enumerate the estate, then four
`InventoryDetails` calls per matched server for CPU, memory, storage and
NICs. OME sources that detail from each server's iDRAC, so it looked like
the same data at a fraction of the connection count.

Three live-appliance runs said otherwise. Every hardware field that
mattered came back in a shape that had to be guessed at:

- `serverStorage` is not a valid `InventoryType` and returns HTTP 400. The
  real one is `serverArrayDisks`.
- `serverArrayDisks` populated none of the expected capacity fields, and
  the one it did populate (`Size`) was in an ambiguous unit — a 1.92 TB
  disk reported ~1.9e6. Capacity ended up parsed out of the Dell **model
  string** (`"... U.2 1.92TB"`).
- `MediaType` was `UNKNOWN` for NVMe drives, so media had to be inferred by
  scanning `MediaType`, `BusType` and the model string together.
- `serverProcessors` carried no logical-processor count and no
  hyperthreading flag, so thread count fell back to `2 x cores`.

Each of those is a heuristic standing in for a value the hardware knows
exactly. `docs/dell-collectors.md` still carries an open request for one
raw `serverArrayDisks` and one `serverProcessors` entry to replace them.

Meanwhile ADR-0016 built a standalone Redfish collector that reads
`CapacityBytes`, `MediaType`, `TotalThreads` and the rest directly from a
BMC, as measured values, and iDRAC is a conformant Redfish implementation
squarely in that collector's stated scope.

## Decision

Collect Dell in two passes, from the two sources that actually know:

1. **OME says who exists.** Two bulk calls, unchanged, give each server its
   profile name, deployment template (SPT), service tag and iDRAC address.
2. **Each server's BMC says what it is.** The discovered addresses become
   `RedfishTarget`s and the whole hardware pass is
   `app.infrastructure.providers.redfish` unchanged.
3. The two halves are joined on the BMC host, and OME's identity fields are
   put back onto the collected server.

The split follows what each side can see. Only OME knows a server is
`ocp4-nyc-prod-worker-03` — an iDRAC has never heard of that name, and the
name is what site parsing and classification key off. Only the BMC reports
hardware as measured values.

`RedfishStandaloneProvider` already takes its `targets` injected and
`RedfishTarget.name` already flows to
`system_to_provider_server(override_name=...)` — a hook that exists because
ADR-0016 faced the same problem from the other side (an operator who knows
the name, a BMC that does not). No change to the Redfish collector was
needed to reuse it here.

### What this deletes

The OME hardware mappers, ~400 lines: `cpu_from_processors`,
`memory_bytes_from_modules`, `storage_from_devices`, `nics_from_interfaces`
and every capacity/media/thread heuristic above. They existed only because
OME did not report the real values. Keeping them as a fallback was
considered and rejected: it would have kept the heuristics alive, and made
a server's hardware source vary run to run depending on whether its BMC
answered — the hardest kind of inconsistency to debug months later.

`idrac_bmc_address` is deliberately **kept**. The Redfish collector reports
`bmc_address_raw` as `https://<host>`; a Dell server's stored address must
stay the `idrac-virtualmedia://...` form that
`app.domain.value_objects.bmc_address.parse_bmc_address` documents and a
Metal3 `BareMetalHost` round-trips into `spec.bmc.address`. Collecting over
Redfish must not silently downgrade it.

## Consequences

**The cost inverts, and this is the real price.** Dell collection was one
appliance answering everything. It is now ~25 HTTPS round trips against
every collected server. A 400-server estate is ~10,000 requests per sweep;
at this platform's 10,000-server target it is ~250,000. Accordingly:

- `namePattern` is applied *before* any BMC is contacted. Unlike the
  standalone Redfish collector — where the filter is deliberately disabled
  because a BMC does not know the server's name — it applies here, because
  OME supplies the name. This is the single thing that keeps the design
  affordable on an estate where most Dell servers are not ours.
- The CronJob moves from hourly to every 6 hours, matching
  `redfishStandalone`. ADR-0016's warning applies unchanged: embedded
  management hardware degrades when polled.
- The collector pod now needs egress to the whole Dell BMC network, not
  just to the appliance.

**Two logins for one manager type.** This breaks the "one endpoint and one
login per `ManagerType`" invariant in CLAUDE.md, knowingly:
`INVENTORY_OME_USERNAME`/`_PASSWORD` for the appliance and
`INVENTORY_OME_BMC_USERNAME`/`_PASSWORD` for the iDRACs. One shared
read-only iDRAC account for the estate; per-host credentials remain the
standalone collector's job, via its inventory file.

Every other BMC knob — timeouts, budgets, fleet concurrency, the
auth-failure guard, the TLS floor and CA bundle — is the shared
`redfish_*` settings rather than a second `ome_*` set. Same protocol,
same class of device; two sets would drift.

**TLS verification is off by default here** where the standalone collector
leaves it on. iDRACs ship a factory self-signed certificate and this
collector's fleet is whatever OME reports, so there is no per-host place to
name a CA. `INVENTORY_REDFISH_CA_BUNDLE` plus
`INVENTORY_OME_BMC_VERIFY_TLS=true` is the scalable fix and the documented
intent, not leaving verification off forever.

**Partial runs stay honest.** A profile OME gives no iDRAC address for, and
every per-host failure the Redfish pass records, both land in
`collection_errors`, so `tools.run_collector` reports PARTIAL rather than a
complete success over a fleet it only half reached.

**Correlation is unchanged.** `IngestService` correlates on
`(vendor, serial_normalized)`. iDRAC reports the service tag as
`SerialNumber`, the same value OME reports as `DeviceServiceTag`, so
servers already ingested by the previous collector are updated in place
rather than duplicated. `vendor_from_manufacturer` maps `"Dell Inc."` to
`Vendor.DELL`, so they do not land as `STANDALONE`.

## Status of verification

**Unverified against real hardware.** This is a design change made against
the Redfish collector's contract and OME's known discovery fields; no live
OME appliance or iDRAC has been collected through the new path. Before
trusting it in production, confirm on real hardware:

1. That iDRAC's `SerialNumber` is the service tag, exactly matching OME's
   `DeviceServiceTag`. If it is not, servers will duplicate rather than
   update, and that is the highest-consequence assumption here.
2. That one read-only iDRAC account authenticates fleet-wide — the Redfish
   auth guard disables a credential after 3 hosts reject it and aborts the
   run after 10 failures, so a partially-deployed account fails loudly.
3. Sweep wall-clock at real fleet size against the 6-hour cadence and
   `redfish_run_budget_seconds`.
4. That the `idrac-virtualmedia://` address still round-trips for the
   servers now collected over Redfish.
