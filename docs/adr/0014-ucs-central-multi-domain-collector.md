# 14. UCS Central collector for multi-domain Cisco fleets

Date: 2026-08-15

## Status

Accepted. **Not yet validated against a live UCS Central** — see
"What is still unproven", which is the section to read before trusting
this in production.

## Context

The UCS Manager collector (ADR-0009) reaches exactly one domain. That is
a property of the product, not of our code: a `UcsHandle` authenticates
to one Fabric Interconnect pair, and `EnvConnectionResolver` supplies one
endpoint per `ManagerType`. A fleet spread over several UCS domains was
therefore unreachable past the first, and the operator asking for this
has roughly 700 Cisco servers across several domains.

Two ways to fix it:

1. **Multi-endpoint UCS Manager.** Accept a list of domain endpoints and
   fan out. Keeps the live, authoritative, emulator-validated data path.
   Costs one credential pair per domain, forever.
2. **UCS Central.** Cisco's own aggregator: domains register with it, and
   it holds a replica of each one's inventory behind a single endpoint
   and login. Tested by Cisco to 10,000 servers across roughly 70–125
   domains.

The initial recommendation here was (1), because Central's replica raised
an unresolved question about service profiles (below). The operator chose
Central, having been shown that risk. This ADR records the decision, the
evidence gathered for it, and the instrumentation added because the risk
could not be eliminated up front.

Scale was *not* a factor either way. The collector's queries are
domain-wide, not per-server: one domain costs the same ~11 HTTP round
trips whether it holds 10 servers or 500. Eight domains via UCS Manager
is ~88 round trips, parallelizable. Central's advantage is configuration
and credential management, not throughput.

## Evidence

Cisco's UCS Central documentation is thin — the XML API guide's own
pages 403 to automated fetches, and `developer.cisco.com` still labels
the SDK an "Alpha release". So the SDK was treated as the specification,
held to ADR-0009's bar: confirm against the installed package source, not
documentation summaries.

The installed `ucscsdk==0.9.0.10` is **byte-identical** to
github.com/CiscoUcs/ucscsdk master at `6c9a34f` ("Updated version for
schema 2.1(1c)", 2025-08-26) — verified by `diff -rq`. Reading the
installed package is reading master.

**Confirmed:**

- **DN shape.** `docs/ucscsdk_ug.rst` states it directly: a chassis in
  domain 1009 is `compute/sys-1009/chassis-1`, composed of
  `computeResourceAggrEp` -> `computeSystem` (`rn="sys-[domainId]"`) ->
  `equipmentChassis`. The mometa agrees. One domain-wide query therefore
  spans every domain, and each object's DN says which domain it came
  from.
- **Property parity with `ucsmsdk`.** Every attribute the mapping reads —
  `presence`, `assigned_to_dn`, `total_memory`, `num_of_cpus`,
  `mgmtIf.ext_ip`/`access`, `adaptorExtEthIf.switch_id`/`peer_dn`,
  `lsServer.type`/`oper_src_templ_name` — exists under the same name in
  `ucscsdk.mometa.*`. The `presence` enum is the same set minus
  `equipped-deprecated`; `LsServer.type` restricts to the same three
  values. This is why the collector reuses `..ucs_manager.mapping`
  wholesale instead of reimplementing it.
- **Where a domain's address lives.** `ComputeSystem.address` — confirmed
  by Cisco's own `ucscsdk/utils/ucscdomain.py`, whose `get_domain()`
  filters `ComputeSystem` on `(address, ..., type="eq")`. `extpolClient`
  (`extpol/reg/clients/client-<id>`) carries registration state, which the
  same file's `_is_domain_available()` checks for `oper_state ==
  "registered"`.
- **No timeout exists in the SDK.** `UcscHandle.__init__(ip, username,
  password, port=443, proxy=None)` takes none, and `ucscsession.post`
  calls `ucscdriver.post(uri, data, read)` without forwarding one, so
  `urlopen` runs with `timeout=None`. Unlike `ucsmsdk`, which accepts
  `timeout`.
- **Server-side filtering is available.** `query_classid(class_id,
  filter_str=...)` with types `eq|ne|ge|gt|le|lt|re` (`re` maps to
  `WcardFilter`), plus a case-insensitivity flag. Deliberately unused —
  see Decision.

## Decision

Build `app.infrastructure.providers.ucs_central` as a peer of
`ucs_manager`, registered in `_PROVIDER_FACTORIES` with its own CronJob.
`UCS_CENTRAL` stops being "a discovery parent, not an inventory source"
and becomes a collection source in its own right.

**Shared logic is extracted, not duplicated.** The grouping and BMC
selection rules were each wrong in ways only a live emulator exposed
(ADR-0009); two copies guarantees the next such fix lands in one of them.
`ucs_common.py` now holds `is_equipped`, `group_by_owning_server_dn`,
`bmc_interface` and `partition_profiles`, and `ucs_manager` imports them.
Its 87 existing tests passing unchanged is the evidence the move was
faithful. These functions work on relative DN structure — ancestry,
prefix containment — never an absolute root, which is exactly why a
`compute/sys-1009/...` tree groups the same as a `sys/...` one.

**The name filter is not pushed into `filter_str`,** despite the SDK
supporting it. The name a server is filtered on lives on its `lsServer`
service profile, not on the compute MO, so a server-side filter could
narrow only one of seven queries while every join still needs the full
compute inventory. More importantly it would put a second, subtly
different copy of "which servers are mine" beside
`run_collector._NameFilteredProvider`, which already applies
`INVENTORY_COLLECTOR_NAME_PATTERN` for every vendor. One filter, applied
once. Revisit only if payload size measurably hurts.

**A timeout is imposed by the wrapper** since the SDK offers none.
`asyncio.wait_for` cancels the await but not the worker thread, so a
timed-out call leaks a thread — acceptable only because a collector run
is a short-lived CronJob process, with `activeDeadlineSeconds` as the
outer backstop. The alternative is a collector that hangs until
Kubernetes kills it with no logged reason.

## What is still unproven

**Whether Central's `lsServer` includes domain-*local* service profiles,
or only the global ones Central owns.** This single question decides
whether the collector works at all, because a UCS server's name comes
from its service profile — `computeBlade.name` is empty in practice
(ADR-0009) — and the name is what carries the site token, the
classification pattern, and the `^ocp` match.

### The schema says yes

A closer read of the SDK found strong evidence, though not runtime proof.
`LsSPMeta` (rn `spmeta`) is a **child of `lsServer`**, and it carries:

```
ownership_state      ['delete-pending', 'disassoc-pending',
                      'global-controlled', 'localized']
globalization_state  ['Globalized', 'globalizing', 'no-op']
operation_code       [..., 'globalization', ...]
```

`localized` means a profile owned by its own domain; `global-controlled`
means owned by Central. Those values are meaningless unless Central's
MIT can hold both kinds — a `localized` ownership state on a child of
`lsServer` only has something to describe if `lsServer` enumerates local
profiles. "Globalization" is Cisco's own term for taking a domain-local
profile under Central's control, and you cannot globalize a profile
Central cannot see.

`LsBinding` (also a child of `lsServer`, rn `pn`) independently carries
`pn_dn`/`assigned_to_dn`, the profile-to-physical-server link, in
Central's own tree.

Weighed against that, the earlier doubts are weak: `LsServer.mo_meta.
parents` being `['computeTemplate', 'orgOrg']` is also true of UCS
Manager's `lsServer`, so it distinguishes nothing; and the SDK blurb's
"global service profiles" describes what the SDK *manages*, not what
Central *replicates*.

So the expected answer is **yes, Central holds local profiles too**. It
is still schema evidence rather than a live run: it proves the model
supports them, not that a given deployment's plain
`query_classid("lsServer")` returns them (inventory sync state and read
privileges could still intervene).

### How to settle it

`tools/verify_ucs_central.py` answers it against a real Central in one
read-only run — no MongoDB, no ingest, no writes:

```
uv run python -m tools.verify_ucs_central
```

It reports registered domains with sync state, the `ownership_state`
breakdown across every `lsSPMeta`, how many servers resolve a
service-profile name, how many match
`INVENTORY_COLLECTOR_NAME_PATTERN`, and a GOOD / PARTIAL / BAD verdict.
A `localized` count above zero with every server named settles this
section affirmatively; update this ADR when it has been run.

If Central does not replicate local profiles, every affected server falls
back to a chassis-slot DN for a name, fails the `^ocp` filter, and the
inventory comes back mysteriously empty.

Rather than guess, the provider **instruments the question**. Every run
logs `ucs_central.domain_summary` per domain — reported vs collected
server counts, `inventory_status`, `last_refreshed_ts`, and how many
servers resolved a profile — and raises a loud
`ucs_central.domain_without_profiles` warning naming the domain and the
likely cause when a domain yields servers but zero profiles. A
`ucs_central.servers_in_unlisted_domain` warning covers inventory for a
domain absent from `computeSystem`. The first real run answers the
question in one log line instead of one silent empty inventory.

Also unproven, and inherited rather than new: the `total_memory` MB
assumption is the same open item ADR-0009 left; CPU model string and
per-drive storage detail were too, until the update below. Central adds
one open item of its own — replication lag, which `last_refreshed_ts`
now surfaces.

### Update (2026-08-16): CPU model and storage detail

`cpu_model`/`storage_drives`/`storage_total_bytes` are now populated —
see ADR-0009's own update, which this collector inherits unchanged since
it shares `..ucs_manager.mapping`. `ComputeBoard`/`ProcessorUnit`/
`StorageController`/`StorageLocalDisk` were confirmed to exist as real
classes in `ucscsdk` itself, property-identical to `ucsmsdk` (same bar as
every other field in this ADR's Evidence section), so no per-domain
fallback to UCS Manager is needed for either field — two more
domain-wide queries (`processorUnit`, `storageLocalDisk`), joined the
same ancestor-walk way as everything else, cover every domain Central
knows about in one pass (seven queries -> nine). This closes a gap a
sibling project's own Cisco collector worked around by logging into each
domain's UCS Manager separately for exactly this data — a real but
avoidable cost this collector doesn't need to pay.

## Consequences

- A multi-domain Cisco fleet is reachable with one endpoint and one
  credential pair.
- Running both Cisco collectors at once double-counts: the same machine
  arrives with two `manager_id`s and two external-id roots
  (`compute/sys-<id>/...` vs `sys/...`). Pick one per fleet, or scope
  them to disjoint domains. The Helm chart defaults `ucsCentral.enabled`
  to `false`, so this requires a deliberate act.
- Servers carry `manager_id = mgr_ucs_central` rather than one per
  domain. The owning domain remains recoverable from `external_id`, and
  `domain_id_from_dn` parses it. Per-domain `Manager` documents were not
  built — no requirement asked for it, and it would change
  `run_collector.manager_for`'s one-document-per-type contract.
- `ucscsdk` joins `ucsmsdk` in the air-gapped mirror
  (`requirements.txt`/`pylock.toml` regenerated). It is Alpha-labelled by
  Cisco and moves slowly — one release in the past year — which the
  quarterly CI pass (ADR-0013) should watch.
- Data is a replica, not live. `last_refreshed_ts` is logged per domain so
  staleness is observable rather than assumed.
