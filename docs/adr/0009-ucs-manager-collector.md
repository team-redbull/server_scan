# ADR-0009: First real vendor collector — Cisco UCS Manager

## Status

Accepted

## Context

Every slice through slice 7 operated on `FakeProvider`
(`app.infrastructure.providers.fake`) — deterministic synthetic data
through the real `ServerInventoryProvider`/`ProviderServer` seam
(`app.domain.ports.provider`), but never a real vendor API. Phase 1's
last remaining gap before the platform can inventory anything real is a
real collector.

Cisco UCS Manager was chosen to go first — not because its API is the
simplest of the four target vendors (Dell OpenManage Enterprise and HPE
OneView are both plainer session-token REST; Cisco Intersight's HMAC
request-signing is comparable in complexity), but because it's the only
one with a real, officially-provided way to test against it without
physical hardware: Cisco's UCS Platform Emulator (UCSPE) runs the actual
UCS Manager binary against a simulated hardware backplane and answers
real XML API calls with realistic inventory data — free with a Cisco.com
login, no support contract. Building the hardest-to-integrate API first,
while it's also the only one buildable and testable end-to-end without
waiting on production access, was worth more than starting with the
easiest API and being unable to verify it against anything real.

## Decision

### API surface used

Every attribute name below was confirmed against the *installed*
`ucsmsdk==0.9.27` package's generated MO source
(`ucsmsdk/mometa/**/*.py`'s `prop_meta` dicts) — not just documentation —
specifically because this was built without a live UCS Manager domain in
this environment to test against interactively.

- Auth: `UcsHandle.login()`/`.logout()` (wraps `aaaLogin`/session cookie).
- Inventory: `configResolveClass` against `computeBlade` and
  `computeRackUnit` — one mapping function handles both (`mapping.
  compute_unit_to_provider_server`), since they share the same relevant
  property set.
- Service profile → template: `computeBlade.assigned_to_dn` → `lsServer`
  (service profile) → `lsServer.src_templ_name`, with
  `ProviderServer.profile_template_external_id` taken from the profile's
  own resolved `oper_src_templ_name` DN. Matches the vendor mapping
  already documented on `app.domain.models.server.ProfileTemplate`.
- CIMC/BMC address: `mgmtIf`, filtered to `access == "out-of-band"`,
  using `ext_ip`. Not every server has one configured; `None` in that
  case is correct data, not a gap.
- NICs and fabric attachments: `adaptorHostEthIf` — `mac` for
  `nic_macs`, `switch_id` ("A"/"B"/"NONE") for `ProviderAttachment.fabric`.

All five queries are domain-wide `configResolveClass`, joined client-side
by distinguished name. See "Corrections" below for why the per-server
`configResolveChildren` this originally used was not merely slower but
wrong, and why there is no `lsServiceProfileTemplate` query.

### Scope cuts, made explicitly rather than silently

- `cpu_model` and `storage_drives`/`storage_total_bytes` stay at their
  zero/`None` defaults — no MO for per-CPU model string or per-drive
  detail was confirmed while building this. `mapping.py`'s module
  docstring tracks this as an open item for the first pass against a real
  domain, not a forgotten field.
- `total_memory`'s unit (MB, converted to bytes) is based on UCS
  Manager's own GUI column label ("Total Memory (MB)"). This one cannot
  be settled from the SDK at all: `prop_meta["total_memory"]` is a bare
  `uint` with no unit annotation, doc string or range — the package is
  code-generated from the MIT schema and carries no unit metadata for
  any property. It stays an assumption until UCSPE or real hardware.
- `ProviderAttachment.fabric_name`/`fabric_id`/`fabric_model`/
  `fabric_serial`/`server_port`/`fabric_port`/`speed_mbps` stay `None` —
  resolving the fabric interconnect's own identity needs a
  `networkElement`/`fabricSwitch`-family lookup whose shape wasn't
  confirmed either.

All of the above are meant to be filled in during the first real pass
against UCSPE or a real domain, not treated as done.

### Async wrapper over a synchronous SDK

`ucsmsdk` has no async support at all. `UcsManagerClient`
(`app.infrastructure.providers.ucs_manager.client`) dispatches every
`login`/`logout`/`query_classid` call through
`asyncio.to_thread`, matching the "never block the event loop" discipline
`app.infrastructure.mongodb`/`app.infrastructure.redis` already hold via
their own native async drivers — substituted with a thread offload here
since no async Cisco SDK exists to reach for instead. One
`UcsManagerClient` (and its `UcsHandle`) per manager per collector run,
never shared across concurrent tasks.

### Credential resolution: a new seam, deliberately minimal

`Manager.credential_ref`/`bmc_credential_ref` (`app.domain.models.
manager`) existed since slice 1 as opaque name fields with nothing that
read them. `CredentialResolver` (`app.domain.ports.credentials`) is the
new port — a `Protocol`, matching `ServerInventoryProvider`'s own pattern,
so production wiring can swap the implementation without touching
collector code.

`FilesystemCredentialResolver` (`app.infrastructure.credentials.
filesystem`) is the only implementation: reads
`{credentials_dir}/{credential_ref}/{username,password}` as two separate
files, matching exactly how Kubernetes projects a `Secret` as a volume
(each key becomes a file). Chosen over per-`credential_ref` environment
variables specifically because a CronJob's pod spec doesn't need editing
every time a new manager is onboarded — only its volume's `projected`
sources list grows (see the Helm chart's `collectors.ucsManager.managers`
list, which generates exactly that). A real secrets-operator integration
(Vault Agent Injector, External Secrets Operator) is a legitimate later
upgrade, but adds a dependency Phase 1 doesn't need yet — the resolver's
`Protocol` boundary is what keeps that swap contained to one new
implementation file, not a scattered rewrite.

### Deployment: one CronJob per manager type, sharing the API's image

`tools/run_collector.py --manager-type UCS_MANAGER` looks up every
enabled `Manager` document of that type, resolves credentials, and runs
each through the exact same `IngestService` pipeline `tools/
seed_inventory.py` already exercises with fake data — classify,
health-evaluate, audit, and upsert in one write, per server. One
manager's failure (unreachable, bad credentials, an unexpected response)
is logged and counted, never aborts the run for the *other* managers of
the same type.

The container image is the same one the API Deployment already uses —
`Containerfile` now also copies `tools/`, and the CronJob overrides
`command`/`args` rather than building a second image. A second,
purpose-built collector image would duplicate the entire dependency
layer for no real isolation benefit at this scale.

## Consequences

- `ManagerType.UCS_CENTRAL` has no collector and isn't expected to get
  one directly — it's a domain-discovery parent over one or more
  `UCS_MANAGER` children (see `Manager`'s own docstring on the
  UCS-Central-then-UCS-Manager two-hop login flow), not itself a source
  of server inventory. `tools/run_collector.py`'s `_PROVIDER_FACTORIES`
  has no entry for it deliberately.
- `OPENMANAGE`, `INTERSIGHT`, and `ONEVIEW` managers get a clear
  `NotImplementedError` from `tools.run_collector` rather than silently
  doing nothing — the next vendor to build reuses this same shape
  (`ServerInventoryProvider` implementation + an entry in
  `_PROVIDER_FACTORIES` + a CronJob).
- The scope cuts above (CPU model, storage detail, fabric interconnect
  identity, the memory-unit assumption) are real, tracked gaps — the
  first collector run against Cisco's UCS Platform Emulator or a real
  domain should specifically verify each one, not just confirm the run
  doesn't crash.
- `requirements.txt`/`pylock.toml` (the air-gapped mirroring exports)
  were regenerated to include `ucsmsdk` and its own dependencies
  (`pyparsing`, `six`, `setuptools`) — anyone mirroring this repo for an
  air-gapped build needs to re-pull those too.

## Corrections (post-build review)

A multi-agent review of this collector — every claim re-verified against
the installed `ucsmsdk==0.9.27` source rather than documentation — found
that two of the assumptions recorded above were not merely unverified but
wrong, in ways that would have made the collector return no useful data
against a real domain. Both are fixed; the reasoning is kept here because
the *shape* of each mistake is the reusable lesson for the next vendor.

**1. `lsServiceProfileTemplate` is not a class in UCS Manager's model.**
`ucscoreutils.find_class_id_in_mo_meta_ignore_case("lsServiceProfileTemplate")`
returns `None`, and there is no `mometa/ls/LsServiceProfileTemplate.py`.
UCS Manager models templates as `lsServer` with `type` in
`{initial-template, updating-template}` (vs `instance`) — `LsServer.
prop_meta["type"]` restricts to exactly those three values.
`query_classid` passes an unrecognized class ID straight through to the
server (there is a literal `# ToDo - How to handle unknown class_id` in
`ucshandle.py`), so this either aborted the whole run for that domain with
a `UcsException` or silently returned nothing. The single `lsServer` query
is now partitioned by `type`, which is both correct and one query fewer.

The lesson: the original build verified every *attribute* against
`prop_meta` but never verified that the *class names* themselves resolve.
Confirming attributes on a class that doesn't exist proves nothing.

**2. `mgmtIf` and `adaptorHostEthIf` are grandchildren of a compute unit,
not children — so the per-server `configResolveChildren` matched nothing.**
From the MO metadata:

    adaptorHostEthIf  parents=['adaptorUnit']                        rn=host-eth-[id]
    adaptorUnit       parents=['computeBlade','computeRackUnit',...]  rn=adaptor-[id]
    mgmtIf            parents=['adaptorHostEthIf','mgmtController']   rn=if-[id]
    mgmtController    parents=[...,'computeBlade','computeRackUnit']  rn=mgmt

Real DNs are therefore `sys/chassis-1/blade-1/adaptor-1/host-eth-1` and
`sys/chassis-1/blade-1/mgmt/if-1` — two levels below the server.
`ConfigResolveChildren`'s class filter applies to immediate children;
`hierarchy=True` does not widen that depth, it only asks the server to
attach each *matched* object's subtree, which `ucscoreutils.
extract_molist_from_method_response` then flattens with no class filter at
all (so a match would return foreign MO classes mixed into the list, which
the mapping would blindly read `mac`/`switch_id` off). Cisco's own SDK
confirms the intended shape: its blade → `mgmtIf` lookup in
`ucsmsdk/utils/ucskvmlaunch.py` uses `configScope`, not
`configResolveChildren`, and `ucsmsdk/utils/inventory.py` collects
adapters with a domain-wide `query_classid`.

The consequence had this shipped: no BMC address, no NIC MACs and no
fabric attachments for any server — and because NIC MACs feed identity
correlation in `IngestService`, degraded server matching rather than just
missing detail.

Both are now domain-wide `query_classid` calls joined client-side by DN
prefix, the same pattern already used for service profiles. This also
removes an N+1 that mattered independently: the old shape issued two
round trips per server (20,000 sequential XML calls at this platform's
10k target), where the new one is a fixed five per manager regardless of
fleet size. The DN-prefix join is exact, not heuristic — `ucsmo.py` builds
every MO's `dn` as `parent_dn + "/" + rn` — and is separator-anchored and
longest-prefix-wins so `sys/rack-unit-1` cannot swallow
`sys/rack-unit-10`'s descendants.

### Smaller fixes from the same review

- **Session leak on login failure.** `list_servers` called `login()`
  *outside* its `try`, so the `finally: logout()` never ran for it.
  `ucssession._login` sets the session cookie and only then runs its
  version and domain-name probes, either of which can raise with the
  session already live server-side — leaking it until UCS Manager's
  server-side timeout, against a per-user session cap.
- **`OSError` escaped the client's error normalization.** `ucsdriver.post`
  re-raises urllib's errors untouched, and `URLError`/`socket.timeout` are
  `OSError` subclasses belonging to neither SDK exception tree. `login()`
  already caught it; the query path did not, so a mid-collection network
  drop escaped raw, contradicting the client's own documented contract.
- **Three logins per manager per run.** `tools/run_collector.py` called
  `provider.health_check()` explicitly, `IngestService.ingest()` called it
  again as its first step, and `list_servers()` opened a third session.
  Each login is ~4 sequential round trips (auth, then the SDK's own
  is-this-UCSM / version / domain-name probes). The explicit call is gone.
- **`equipped-slave` / `equipped-not-primary` are now excluded.** Both
  pass a `startswith("equipped")` check but are the secondary half of a
  multi-node server (a B460's slave blade), which UCS Manager reports as a
  logical server under the primary's DN — ingesting them double-counted
  one machine as two.
- **Cross-org template name collisions.** Template names are only unique
  within an org, so the name→DN map is lossy by construction. The
  profile's own resolved `oper_src_templ_name` DN is preferred, with the
  by-name lookup kept only as a fallback.
- **Endpoint format is validated up front.** `UcsSession.__create_uri`
  interpolates the endpoint raw into `"%s://%s:%s"`, so a URL or an
  embedded port produces `https://https://host:443` and an opaque
  connection failure. `Manager.endpoint` is a free-form `str`, so the
  client now rejects both with an actionable message.

### Why none of this was caught before

The collector had 45 passing tests, clean `mypy` and clean `ruff`. But
`test_ucs_manager_provider.py` imported exactly one symbol — the five-line
`_is_equipped` helper — and declared the rest out of scope for a unit
test; `client.py` and `tools/run_collector.py` had no coverage at all.
Nothing exercised a single call signature or class name, which is exactly
how a nonexistent class and two structurally-wrong queries shipped green.

The provider, client and collector-runner now have real tests
(`tests/unit/infrastructure/providers/test_ucs_manager_{provider,client}.py`,
`tests/unit/tools/test_run_collector.py`). The provider's fake client
returns MOs whose DNs nest at the *real* confirmed depths, so a regression
back to a per-server child query fails the suite rather than passing it.
That is the standing bar for the next vendor collector: a test whose
fixtures encode the vendor's real object shape, not the shape the
implementation happens to assume.

**Still unverified, and only settleable against UCSPE or real hardware:**
the `total_memory` MB assumption, plus the original scope cuts (CPU model,
storage detail, fabric interconnect identity).

## Validated against real hardware (UCSPE 4.2(2aS9))

Everything above was built without a live domain. It has now been run
end-to-end against a Cisco UCS Platform Emulator instance
(UCSPE 4.2(2aS9), 5 blades + 9 rack units, 14 servers): full collector
run via `tools/run_collector.py`, then the REST API and UI over the
result. **14 fetched, 14 created, 0 errors.**

### Confirmed on real hardware

- **`lsServiceProfileTemplate` does not exist.** Querying it returns
  `UcsException: ERR-xml-parse-error … no class named
  lsServiceProfileTemplate`, which aborts the run for the whole domain —
  exactly the failure the Corrections section predicted from SDK
  metadata. Partitioning `lsServer` by `type` is correct.
- **`mgmtIf` / `adaptorHostEthIf` are not children of a compute unit.**
  `query_children(in_dn="sys/chassis-3/blade-1", class_id="mgmtIf",
  hierarchy=True)` returns **0 objects**, while a domain-wide
  `query_classid` + DN-prefix join finds 6 under that same blade. Real
  DNs: `sys/chassis-3/blade-1/mgmt/if-1`,
  `sys/rack-unit-1/adaptor-1/host-eth-1`. `hierarchy=False` returns 0
  too, so the depth — not the flag — was the problem.
- `presence` is `equipped` on all 14; `switch_id` is exactly `A`/`B`;
  `admin_state` is `enabled`. The prefix check and the A/B/NONE handling
  hold.
- `normalize_mac` correctly rejects the all-zero MAC that UCSPE reports
  for blade CIMCs (`00:00:00:00:00:00` -> `None`), so a meaningless
  address never reaches the document.

### Wrong, and fixed as a result

- **The BMC interface filter selected nothing.** `access == "out-of-band"`
  matched no server interface: a blade's own management interfaces report
  `access="unspecified"` (`subject="blade"`), and the only two
  `out-of-band` interfaces in the entire domain belong to the fabric
  interconnects (`subject="switch"`), which sit under no server's DN.
  Selection is now by position — the interfaces under the server's own
  `{dn}/mgmt/` controller, excluding `in-band`/`internal`/`virtual`,
  preferring `out-of-band` when a domain does set it. This populated real
  CIMC MACs (Cisco OUI `00:25:b5:…`) on all 9 rack units.
- **Only querying `adaptorHostEthIf` left most of the fleet with no
  network data at all.** `adaptorHostEthIf` is a *logical vNIC* that only
  exists once a service profile is associated; `adaptorExtEthIf` is the
  *physical* adapter port, present on every discovered server. In this
  domain the two are strictly complementary — 12 servers had only
  ext-eth, 2 had only host-eth, none had both. Collecting both and
  unioning them per server took the fleet from 6 MACs / 6 attachments
  across 2 servers to **66 MACs / 66 attachments across all 14**, with
  zero servers left without network data. `adaptorExtEthIf.peer_dn` also
  gives the fabric-side port, now mapped to
  `ProviderAttachment.fabric_port`.
- **Fabric path counts were always zero.** `ConnectivityAttachment.
  oper_state` is documented `UP | DOWN | UNKNOWN` and
  `compute_connectivity_facts` counts those exact strings, but the
  provider passed UCS's own vocabulary through untouched (`operable`,
  `admin-down`). A server with four attachments stored
  `fabric_paths_up: 0, fabric_paths_down: 0` — silently disabling the
  connectivity health signal for every UCS server. UCS states are now
  mapped explicitly. `admin-down` maps to `DISABLED`, not `DOWN`: it is
  the normal state of an adapter port on a server with no service
  profile, and `compute_connectivity_facts` counts neither, so an
  unassociated server does not masquerade as a connectivity fault.

### Second pass: with service profiles associated

The first pass ran against a domain with zero service profiles, which
left the profile/template mapping and the server-name path unexercised.
Creating a `updating-template` and instantiating four profiles from it
(`lsInstantiateNNamedTemplate`), then binding them to servers, exercised
both — and exposed one more production-breaking defect.

- **`computeBlade.name` is empty even with a service profile bound to
  it.** The mapping fell back to the DN, so every UCS-sourced server was
  named `sys/chassis-3/blade-1` — a location, not an identity. Since the
  platform reads both the site token and the installation-type
  convention *out of the name*, a UCS fleet would have been permanently
  unsited and unclassified. The associated service profile's name is
  what a UCS server is actually called, and is now preferred; the DN is
  the last resort and remains the `external_id` regardless.
- `oper_src_templ_name` is confirmed to hold the template's resolved DN
  (`org-root/ls-hypershift-five-tmpl`) on a real instantiated profile,
  which is what makes `profile_template_external_id` collision-proof
  across orgs.
- Templates really do come back from the same `lsServer` query as
  instances, distinguished only by `type` — confirming the partition.

With four profiles bound, the full chain resolves end to end:

    ocp4-hypershift-five-01       site=five  HOSTED_CLUSTER  tmpl=hypershift-five-tmpl
    ocp4-hypershift-data-five-02  site=five  HOSTED_CLUSTER  tmpl=hypershift-five-tmpl
    ocp4-prod-one-infra-01        site=one   UPI             tmpl=hypershift-five-tmpl
    ocp4-one-control-plane-02     site=one   UPI             tmpl=hypershift-five-tmpl

and the API filters agree (`?site_id=five` -> 2, `?site_id=one` -> 2,
`?installation_type=HOSTED_CLUSTER` -> 2, `?installation_type=UPI` -> 2).
The ten servers with no profile keep their DN, no site and
UNCLASSIFIED — correct, and visibly distinct from the four that resolve.

### Still not settled

- **The `total_memory` MB assumption remains unproven.** UCSPE reports
  `49152` for all 14 servers regardless of model — consistent with 48 GB
  in MB, but a single synthetic value across every model is weak
  evidence, and the emulator contradicts itself elsewhere (the same
  blade's four equipped `memoryUnit`s report `capacity=65536` each,
  summing to 262144, and its `memoryArray.max_capacity` is 12288). Only
  real hardware will settle this.
- **Association never reached `associated`.** The bound profiles stopped
  at `oper_state="config-failure"` because they carry no boot policy,
  vNICs or UUID pool. `assigned_to_dn` is set regardless, which is all
  the mapping reads, but a fully associated server may expose fields
  this pass never saw.
- `cpu_model` and storage detail remain unmapped (`num_of_cpus`/
  `num_of_cores`/`num_of_threads` do populate correctly).
- Server `name` falls back to the DN (`sys/chassis-3/blade-1`) when a
  server has no service profile, since UCS leaves `name` empty. That in
  turn means `parse_site_code` finds no site token and every server lands
  in the "Unassigned" bucket — correct behaviour for these names, but it
  means a UCS-sourced fleet only gets real sites once hostnames carry
  them.

## Update (2026-08-16): CPU model and storage detail are now mapped

The two scope cuts above are closed, not by new access against UCSPE, but
by the same bar the rest of this module holds every field to: confirmed
directly against the installed `ucsmsdk`/`ucscsdk` package source.
`ComputeBoard`, `ProcessorUnit`, `StorageController` and
`StorageLocalDisk` all exist as real classes, parented off a compute
unit the same grandchildren-or-deeper way `mgmtIf`/`adaptorHostEthIf`
already are (`computeBlade` -> `computeBoard` -> `processorUnit`;
`computeBlade` -> `computeBoard` -> `storageController` ->
`storageLocalDisk`), so `ucs_common.group_by_owning_server_dn`'s
ancestor-walk join handles them without modification — a
`storageController` parented off `equipmentChassis` instead (shared
chassis storage, not one server's) is dropped by the same join, the same
way a chassis-owned `mgmtIf` already was.

`cpu_model` comes from the first equipped `processorUnit.model`.
`storage_drives`/`storage_total_bytes` come from every equipped
`storageLocalDisk`, with `device_type` mapped onto the platform's
`MediaType` and `disk_state` mapped onto `HealthSeverity`. `size`'s unit
gets the same "cannot be settled from the SDK alone" treatment as
`total_memory` — assumed MB, unverified — with one piece of independent,
weak corroboration: a sibling project's own from-scratch Cisco collector
(`team-redbull/ServerScanner`) made the identical MB assumption for this
exact field, discovered while researching how that project structured
its own vendor-provider abstraction.

`processorUnit`/`storageLocalDisk` are two more domain-wide queries per
collector run (six -> eight for UCS Manager). Real-hardware verification
of the `size` unit and of these classes' actual presence/depth against a
live domain is still open, same as `total_memory` always was.

## Update (2026-08-17): the entry point moved, the collector did not

`--manager-type UCS_MANAGER` and its CronJob template were removed. This
ADR is not deprecated by that, and nothing it validated has changed.

What was deleted is the *entry point*: the standalone way to point the
platform at one UCS Manager domain, plus the `INVENTORY_UCS_MANAGER_IP`
setting that named that one domain. `UcsManagerProvider`, its client, its
mapping and its tests are all untouched and run more often than before —
the UCS Central collector now drives them once per registered domain,
with the addresses coming from Central (`ComputeSystem.address`) and
`INVENTORY_UCS_MANAGER_USERNAME`/`_PASSWORD` as the login for each.

The reason that reuse is safe is this document: the UCSPE run recorded
above is the only validation against real UCS Manager behaviour that this
platform has, and every defect it found — the nonexistent MO class, the
BMC filter that matched nothing, the missing adapter interface class, the
always-zero fabric path counts, the chassis-slot naming — is fixed in the
code being reused. Reimplementing this data path against UCS Central's
replica instead would have thrown that evidence away. See
`docs/adr/0014`'s 2026-08-17 update for the design and its costs.
