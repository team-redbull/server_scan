# 14. UCS Central collector for multi-domain Cisco fleets

Date: 2026-08-15

## Status

Accepted. **Validated against a live UCS Central** (2026-08-18, 152
domains) — see the dated update at the end of this file. The remaining
open items are narrower than "does this work at all"; read that update
before trusting this in production.

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

> Narrowed by the 2026-08-17 update at the end of this file, which moved
> collection to each domain's own UCS Manager. The question below is
> still open, but it no longer decides whether the collector works — only
> whether it can *skip* domains safely, and the answer to "don't know" is
> now "collect it anyway". Read this section for the evidence, then that
> update for what the collector actually does with it.

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

> The first two bullets are superseded by the 2026-08-17 update below,
> which removed the standalone UCS Manager collector: there is no longer
> a second Cisco collector to run alongside this one, and the connection
> config is now one endpoint plus *two* logins. That update has its own
> Consequences section.

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

## Update (2026-08-17): Central discovers, UCS Manager collects

This supersedes the design above. The collector no longer reads inventory
from Central's replica at all, and there is no setting to make it do so
again — the replica path is deleted, not disabled.

**What it does now.** Two queries go to Central, regardless of fleet
size: `computeSystem`, for the registered domains and each one's
`address`, and `lsServer`, for the service-profile names and which domain
each profile belongs to. Everything else comes from each domain's own UCS
Manager, collected through `..providers.ucs_manager` unchanged, up to
`INVENTORY_UCS_CENTRAL_DOMAIN_CONCURRENCY` domains at a time.

**At the same time, the standalone UCS Manager collector was removed.**
`--manager-type UCS_MANAGER` is no longer a runnable collector, its
CronJob template is deleted, and `INVENTORY_UCS_MANAGER_IP` is gone from
settings. `UcsManagerProvider` itself is untouched and busier than
before: it is now the engine this collector drives once per domain, and
`INVENTORY_UCS_MANAGER_USERNAME`/`_PASSWORD` are what it logs into every
domain with.

### Why

1. **The detail is live.** It comes from the domain itself, so the
   replication lag flagged in Consequences above stops mattering for
   everything except the profile-name list. The data path is also the one
   ADR-0009 validated end to end against a live UCS Platform Emulator,
   rather than a second path that has never met real hardware — which is
   the whole reason it was reused rather than reimplemented.
2. **It shrinks what rests on the replica to one field.** Only the
   *names* still come from Central, and names are precisely what this
   ADR's "The schema says yes" section argues Central does hold:
   `LsSPMeta.ownership_state` enumerating `localized` alongside
   `global-controlled`, on a child of `lsServer`. It is also the part the
   sibling project team-redbull/ServerScanner depends on in production —
   its `CiscoStrategy` reads `lsServer` from Central for names, then logs
   into `lsServer.domain` with one shared UCS Manager credential pair.
   That is independent operational evidence for both the name list and
   the single-credential assumption on a real deployment.
3. **It closes the deep-inventory question instead of assuming it.** The
   2026-08-16 update above reasoned from `ucscsdk` class definitions that
   Central's replica carries `processorUnit`/`storageLocalDisk`, and the
   same assumption was riding on `adaptorHostEthIf` and `mgmtIf`. Class
   definitions prove the model *can express* those objects, not that a
   given Central populates them. Nothing needs that to be true now.

The cost, stated plainly: one login plus 11 queries per collected domain
(pinned by `test_scales_query_count_independently_of_fleet_size`) instead
of 9 queries in total, and a UCS Manager account that works on every
domain. For the ~8-domain fleet this was built for that is a minute or
two of wall-clock, bounded further by collecting several domains
concurrently. Validated at 152 domains on 2026-08-18 — see the dated
update below for what that run actually cost and found.

### What was deliberately not copied

The sibling project fetches per server: `query_dn(profile.dn)` then
`query_children` for `VnicEther`, `VnicIpV4PooledAddr`, `ComputeBoard`,
`ProcessorUnit`, `StorageController`, `StorageLocalDisk`. That is O(number
of servers) round trips. This collector keeps the domain-wide
`query_classid` + client-side DN join that ADR-0009's module docstring
justifies — O(number of classes) per domain, independent of server count,
and correct for the grandchild classes a DN-scoped
`configResolveChildren` does not match at all.

### The new failure mode, and the guard against it

Pruning. If Central does *not* list a domain's local profiles, that
domain's names are invisible here, and a name-pattern check would skip a
domain that in fact holds the fleet — trading the old "servers arrive
unnamed" symptom for a quieter "servers never arrive".

So the rule is asymmetric: a domain is skipped **only** when Central
lists profiles for it and none match
`INVENTORY_COLLECTOR_NAME_PATTERN`. A domain Central lists no profiles
for is always collected. Absence of evidence never prunes; only positive
evidence of a non-match does. Pruning stays an optimisation, never the
source of truth — `run_collector._NameFilteredProvider` still applies the
same pattern to every server that comes back, so a domain wrongly kept
costs one wasted login and nothing else.

### Identity

A UCS Manager DN is domain-local: `sys/chassis-1/blade-1` exists in every
domain, so using it verbatim would collide across domains. Collected
servers therefore have their `external_id` rewritten to
`compute/sys-<domainId>/...`, the same form the replica path emitted, so
`Server.external_ids[mgr_ucs_central]` still names exactly one machine
and still identifies its owning domain. (Identity correlation itself is
vendor + normalized serial — see `IngestService` — so the DN was never
the dedup key; this is about the recorded reference staying meaningful,
and about documents ingested before this change still matching.)

### Consequences of this update

- **A domain not registered with UCS Central is uncollectable.** There is
  no longer any way to point this platform at a single UCS Manager. That
  is a real capability loss, accepted knowingly: the fleet this serves is
  fully registered with Central, and keeping a second entry point meant
  keeping a second way to configure, deploy and mis-deploy Cisco
  collection.
- **Central is now a hard single point of failure for all Cisco
  collection.** Previously it was one of two routes to a domain; now
  Central being down, unreachable, or wrong about the domain list stops
  every Cisco collection, including for domains that are themselves
  perfectly healthy. There is no fallback path, and adding one means
  restoring the removed entry point.
- Both of the above are recoverable by reverting this commit —
  `UcsManagerProvider`, its client and its tests are all still present
  and exercised, so what would need rebuilding is the CronJob template,
  the `_PROVIDER_FACTORIES` entry and the `ip` setting, not the collector.
- A domain that rejects the shared UCS Manager login fails that domain
  alone and is logged; the rest of the run continues.

### Still unproven

- ~~Whether Central's `lsServer` lists domain-local profiles~~ —
  answered for the validating fleet by the 2026-08-18 update below:
  **zero** `localized` profiles exist there, every one is
  `global-controlled`. The schema still supports `localized` (unchanged
  from "The schema says yes" above) and pruning's unknown-case-is-safe
  guard still matters for a fleet that does use domain-local profiles —
  this fleet simply isn't one. `tools/verify_ucs_central.py` remains the
  way to answer it for any other deployment.
- That one UCS Manager credential pair authenticates against every
  registered domain. True for the sibling project's fleet, and now also
  confirmed for the 2026-08-18 validation run below (zero login
  failures across 100 collected domains); still a property of how a
  given fleet's domains are configured, not something Cisco guarantees.
- The `total_memory` MB assumption inherited from ADR-0009.

## Update (2026-08-18): validated against a live UCS Central

`tools/verify_ucs_central.py` and `tools/run_collector.py --dry-run`
were both run against the operator's real UCS Central, 152 registered
domains, ~3346 equipped servers.

**`verify_ucs_central` result:**

```
1. Registered domains: all 152 report inventory_status=ok, and
   REPORTED == SEEN for every one — no stale replicas.
2. lsServer objects returned: 4129 (3613 profiles, 516 templates)
   lsSPMeta objects returned : 4129
   ownership_state breakdown:
     global-controlled       4123  <-- owned by UCS Central (a GLOBAL profile)
     delete-pending              6
   (no "localized" entries at all)
3. servers with a resolved service-profile name: 3265 / 3346
   VERDICT: PARTIAL — 3265 of 3346 servers resolved a name.
```

**This settles the schema question for this fleet, in the direction the
schema evidence left open rather than the one "The schema says yes"
expected:** every service profile Central holds is `global-controlled`.
This operator's fleet is provisioned entirely through Central-owned
global profiles — no domain-local ones exist to replicate at all. That
is not a contradiction of the SDK schema evidence (Central's MIT model
still *supports* `localized`), it is empirical confirmation that this
particular deployment doesn't exercise it. Practically, it is a *good*
outcome for the pruning design: since every profile is Central's own
data rather than a replica of something domain-local, Central's copy of
`lsServer` is authoritative by construction here, and
`domains_to_collect`'s pruning decision carries no replica-lag risk for
this fleet.

**The `PARTIAL` verdict (81 unresolved servers, 2.4%) does not indicate
a Central-replication gap**, for the same reason: with zero local
profiles in play, there is no local-profile-Central-can't-see
explanation available. The far more likely cause is physically-equipped
compute units with no service profile assigned at all — spare or
not-yet-provisioned hardware, which is normal fleet composition at this
scale. `verify_ucs_central` only checks whether *Central's own replica*
resolves a name; the real collector resolves names live against each
domain's own UCS Manager (`ucs_manager/provider.py`), independent of
Central's copy, so an unassociated server would show up nameless there
too — this is not an artifact this collector's design introduces.

**The `--dry-run` result confirmed the live path works**: successful
MongoDB connectivity check, successful login to every domain attempted,
raw `ProviderServer` fields populated as expected (CPU sockets/cores/
threads/model, memory, per-drive storage with model/serial/media
type/health, NIC MACs correctly preferring the vNIC over the physical
port, fabric attachments). One real defect surfaced by this run and
fixed separately: `mgmtIf.ext_ip` came back an unset sentinel for a
server whose CIMC address was in fact assigned via the service profile's
management IP address policy, recorded on `vnicIpV4PooledAddr` rather
than the physical `mgmtIf` — see `docs/cisco-collectors.md`, "BMC and
management interface selection", for the full finding.
`INVENTORY_UCS_CENTRAL_DOMAIN_CONCURRENCY=4` (the default) collected 100
of 152 domains with zero failed domains; 52 were pruned by
`INVENTORY_COLLECTOR_NAME_PATTERN`, which is pruning working as
designed, not a fault.

