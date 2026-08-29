# Cisco collectors — verified implementation facts

This is the technical reference behind
`app.infrastructure.providers.ucs_common`,
`app.infrastructure.providers.ucs_manager`,
`app.infrastructure.providers.ucs_central` and
`app.infrastructure.providers.intersight`. Every docstring in those
modules points here rather than carrying its own explanation, so the
`##` headings below are load-bearing: renaming one breaks the
cross-references in several source files.

It is for whoever is about to change a Cisco collector. Almost nothing
here is inferrable from the code — most of it was established by reading
installed SDK source, and several items cost a live UCS Platform Emulator
run to learn. Each fact is recorded with its provenance, because a fact
without a source becomes folklore nobody dares change.

**Relationship to the ADRs.** `docs/adr/0009-ucs-manager-collector.md` is
the UCS Manager collector's decision record and holds its UCSPE
validation; `docs/adr/0014-ucs-central-multi-domain-collector.md` is the
UCS Central one; `docs/adr/0017-intersight-collector.md` is Intersight's. Those are dated decisions and are not maintained — they
record what was decided and why, at a moment. **This document describes
current behaviour and must be updated when the code changes.** Where a
fact is already recorded in an ADR it is cited here in one line rather
than restated, so the ADR stays the single source for it.

Extracted from the code comments on 2026-08-18, when this repo moved
justification out of inline comments and into documentation (see
`CLAUDE.md`, standing convention 8).

## Shared object model and DN joins

`ucsmsdk` and `ucscsdk` describe the same object model, which is why
`ucs_common` is shared rather than duplicated per provider. Property
parity was verified attribute-by-attribute against both installed
packages — ADR-0014's Evidence section lists the exact attributes
checked. Every attribute the mapping reads was confirmed against the
*installed* `ucsmsdk==0.9.27` generated MO source
(`ucsmsdk/mometa/**/*.py`'s `prop_meta` dicts), not against
documentation.

Only the DN *root* differs: `sys/chassis-1/blade-1` under `ucsmsdk`,
`compute/sys-1009/chassis-1/blade-1` under `ucscsdk`. Every function in
`ucs_common` works on relative structure — ancestry, prefix containment —
and never on an absolute root, which is what lets both SDKs share them
unchanged.

`computeBlade` and `computeRackUnit` carry the same relevant property
set, so one mapping function handles both.

**Why shared and not copied.** The grouping and BMC-selection rules were
each wrong in a way that only a live UCS Platform Emulator exposed
(ADR-0009's validation sections). Two copies guarantees the next such fix
lands in only one of them.

### Everything is a domain-wide query joined client-side

Every lookup either collector makes is a domain-wide `query_classid`
joined client-side by distinguished name, never a per-server
`query_children`. **This is a correctness requirement before it is a
performance one.**

`mgmtIf` and `adaptorHostEthIf` are *grandchildren* of a compute unit,
not children, so a `configResolveChildren` scoped to a blade's DN and
filtered to either class matches nothing at all. ADR-0009's "Confirmed on
real hardware" section records the live proof — 0 objects returned
against a blade DN, 6 found by the domain-wide join. The supporting
metadata, confirmed against the installed `ucsmsdk==0.9.27` MO metadata:

```
adaptorHostEthIf  parents=['adaptorUnit']                        rn=host-eth-[id]
adaptorUnit       parents=['computeBlade','computeRackUnit',...]  rn=adaptor-[id]
mgmtIf            parents=['adaptorHostEthIf','mgmtController']   rn=if-[id]
mgmtController    parents=[...,'computeBlade','computeRackUnit']  rn=mgmt
```

so the real DNs are `sys/chassis-1/blade-1/adaptor-1/host-eth-1` and
`sys/chassis-1/blade-1/mgmt/if-1` — two levels down.

**`hierarchy=True` does not fix this.** It does not widen the class
filter's depth; it only asks the server to attach each *matched* object's
subtree, which `ucsmsdk.ucscoreutils.extract_molist_from_method_response`
then flattens with no class filter at all — so a match would return
foreign MO classes mixed into the list. ADR-0009 confirms
`hierarchy=False` returns 0 too: the depth was the problem, not the flag.

**Cisco's own SDK does the same thing.** Its blade -> `mgmtIf` lookup in
`ucsmsdk/utils/ucskvmlaunch.py` uses `configScope`, not
`configResolveChildren`, and `ucsmsdk/utils/inventory.py` collects
adapters with a domain-wide `query_classid`.

**The DN join is exact, not heuristic.** `ucsmo.py` builds every MO's
`dn` as `parent_dn + "/" + rn`, so a descendant's DN always starts with
its owning server's DN followed by a separator.

### `presence` and `is_equipped`

The full enum, from `ComputeBladeConsts`/`ComputeRackUnitConsts` in
either SDK: `empty`, `equipped`, `equipped-deprecated` (`ucsmsdk` only),
`equipped-identity-unestablishable`, `equipped-not-primary`,
`equipped-slave`, `equipped-unsupported`,
`equipped-with-malformed-fru`, `inaccessible`, `mismatch`,
`mismatch-identity-unestablishable`, `mismatch-slave`, `missing`,
`missing-slave`, `unauthorized`, `unknown`.

Every `equipped*` variant is a physically-present server and **no
non-equipped value shares the prefix**, which is what makes the
`startswith("equipped")` test exact rather than a heuristic. This
listing exists nowhere else; the ADRs do not enumerate it.

`equipped-slave` and `equipped-not-primary` are excluded: they are the
secondary half of a multi-node server (a B460's slave blade),
physically present but not independently addressable, and both SDKs
report the logical server under the primary's DN — so ingesting them
double-counts one machine as two. ADR-0009 records the exclusion
decision.

The same enum family is reused on `processorUnit` and
`storageLocalDisk`, so an empty socket or drive bay reporting
`presence="empty"` is skipped by the same test.

### `group_by_owning_server_dn`

A domain-wide `query_classid` returns instances owned by chassis, fabric
interconnects and IO modules as well as servers — `mgmtIf` alone hangs
off about a dozen parent classes — so anything not under one of the
given server DNs is dropped rather than mis-attributed. Objects owned by
something other than a server, such as chassis-level shared storage, are
dropped by the join for free rather than needing their own filter.

The implementation walks **each MO's own ancestor DNs, nearest first**,
rather than testing every server DN as a prefix. Three properties fall
out of that choice, and only the first is in an ADR:

1. Segment-boundary exactness: `sys/rack-unit-1` cannot claim
   `sys/rack-unit-10`'s descendants (ADR-0009).
2. **Nearest-ancestor-wins**: a nested `computeServerUnit` keeps its own
   descendants instead of donating them to the enclosing server.
3. Cost is O(MOs × DN depth), not O(MOs × servers), and DN depth is a
   handful of segments regardless of fleet size.

## BMC and management interface selection

ADR-0009's "Wrong, and fixed as a result" section records this in full:
filtering `mgmtIf` on `access == "out-of-band"` selected **nothing** on a
real domain. Verified against UCSPE 4.2, a blade's own management
interfaces report `access="unspecified"` with `subject="blade"`, and the
only two `out-of-band` interfaces in the entire domain belong to the
fabric interconnects (`subject="switch"`), which are under no server's DN
at all.

Selection is therefore **by position in the tree**: a compute unit owns
exactly one management controller at `{server_dn}/mgmt`, and the
interfaces beneath it are the CIMC's. `out-of-band` is still preferred
when present, for domains that do set it.

**The `_NON_BMC_ACCESS` exclusion set** — `in-band`, `internal`,
`virtual` — is excluded regardless of tree position. These are
`MgmtIfConsts.ACCESS_*` values:

- `in-band` rides the data path rather than the CIMC, so its address is
  not the BMC address even though it hangs off the same controller.
- `internal` is adapter-internal plumbing: the other `mgmtIf`s under a
  server hang off its adapters at `{server_dn}/adaptor-N/mgmt/...`.
- `virtual` is likewise never a physical BMC address.

Everything else is accepted, including the `unspecified` a real blade
reports.

**Address form.** `_bmc_address` emits an `ipmi://{host}:623` URI,
matching the form `app.domain.value_objects.bmc_address.parse_bmc_address`
already recognizes for Cisco — a UCS-managed CIMC's out-of-band interface
is reachable the same way a standalone one is. `0.0.0.0` and `none` are
unset sentinels and yield `None`, not an address, from either source
below.

**The address itself has three possible sources, tried in this order,
and `mgmtIf.ext_ip` is the least reliable one.** Discovered against real
hardware in two passes (2026-08-18), not UCSPE — the emulator never got
a service profile past `config-failure`, so it never exercised any of
this. A fully associated profile assigns the CIMC's out-of-band address
through the service profile's management IP address policy, which UCS
Manager records as a `vnicIpV4PooledAddr` (pool-assigned) or
`vnicIpV4StaticAddr` (static) MO. Both classes list two different valid
parents in the installed `ucsmsdk`'s `mo_meta.parents`: the service
profile's own `lsServer` DN, and the physical compute unit's
`mgmtController` (a sibling of `mgmtIf`, at `{server_dn}/mgmt`).

The **first pass** assumed the compute-unit location, by analogy with
`bmc_interface`'s DN-prefix selection of `mgmtIf` — reasonable from the
schema alone, but empirically wrong: on the domain that surfaced this,
querying `vnicIpV4PooledAddr`/`vnicIpV4StaticAddr` domain-wide and
joining by compute-unit DN prefix found nothing, and the BMC address
still came back missing after that fix shipped. The **second pass**
found the actually-populated location: a **direct child of the service
profile's own DN** (`{profile_dn}/ipv4-pooled-addr`), matching what the
sibling project team-redbull/ServerScanner does in production — its
`CiscoStrategy` queries `VnicIpV4PooledAddr` as a child of the service
profile MO it already has in hand, never of the physical compute unit.

`ucs_common.management_ip_by_parent_dn` indexes every
`vnicIpV4PooledAddr`/`vnicIpV4StaticAddr` carrying a real address by its
immediate parent DN, keeping both a profile-scoped and a
compute-unit-scoped entry possible in the same dict — one domain-wide
pass covers both without a second per-server query. Per server,
`mapping._management_ip_addr` looks up the assigned profile's DN first
and the compute unit's `{server_dn}/mgmt` DN second, so a deployment
that does populate the compute-unit-scoped MO (schema-valid, just not
observed) is still covered as a fallback rather than silently dropped.
`_bmc_address` then tries that resolved MO's `addr` first and falls back
to `mgmt_if.ext_ip` only when neither DN resolved one. On the domain
observed, `mgmtIf.ext_ip` came back an unset sentinel while the
profile-scoped pooled address carried the real one; `bmc_mac` was
unaffected throughout — it always came from `mgmtIf.mac`, populated in
every run. Only the *address* half of the pair was ever missing, and
only because it was being looked for in the wrong of two schema-valid
places.

## Service profiles and server names

A UCS server's name comes from its **service profile**, not
`computeBlade.name`, which is an optional user label empty in practice —
verified against UCSPE 4.2, where a blade with a profile bound to it
still reported `name=""`.

ADR-0009 records the consequence: falling back to the DN names every
server `sys/chassis-3/blade-1`, a location rather than an identity, which
carries neither the site token (`app.domain.value_objects.site`) nor the
installation-type convention the classification rules match on. A
UCS-sourced fleet would be permanently unsited and unclassified. The DN
remains the last-resort name, and stays the `external_id` regardless —
identity and display name are different jobs.

### The profile's own DN doubles as its org path

`ProviderServer.profile_dn` carries the assigned `lsServer`'s own DN
verbatim (e.g. `org-root/org-five/ls-worker-01`), populated in
`compute_unit_to_provider_server` alongside the template fields. UCS
Manager organizations nest as DN segments, so this single string is both
"which service profile" and "which org tree it lives in" — there is no
separate org field to carry. It is distinct from `profile_template_*`:
that pair names the reusable template a profile was created from, this
names the one profile instance bound to this specific server. Currently
surfaced only in `tools/run_collector.py`'s `--dry-run` print, not
threaded into `IngestService`/`Server` — adding persistence is a
separate decision (`Server` model, MongoDB, API, UI) that hasn't been
asked for yet.

### Profiles and templates share one class

`lsServer` carries both real service profiles and the templates they
derive from, distinguished **only** by the `type` attribute. There is no
separate `lsServiceProfileTemplate` class in either model — confirmed two
ways against the installed packages: both SDKs'
`find_class_id_in_mo_meta_ignore_case` return `None` for that name, and
`LsServer.prop_meta["type"]` restricts to exactly `initial-template`,
`updating-template` and the normal profile type in both. Querying the
non-existent class name aborts the whole run (ADR-0009). One query
returns both kinds; partitioning happens in
`ucs_common.partition_profiles`.

### Template resolution is by DN, not by name

A bare template name is unique only *within* one org — two orgs can each
own a `worker-template` — so the template's full DN (e.g.
`org-root/ls-template-mytemplate`) is the stable identifier.
`partition_profiles` returns template DNs keyed by bare name, and that
mapping is therefore lossy across orgs by construction.

`oper_src_templ_name` is UCS Manager's own resolved absolute DN for the
source template, following the `oper*` convention across `lsServer`'s
policy-name properties, so it is preferred: it is collision-proof across
orgs where the by-name lookup is not. Resolution order is
`oper_src_templ_name` -> by-name lookup -> bare name, the last two
covering a profile whose template was since deleted or renamed.

### `LsServer.domain` — the domain link on UCS Central

The installed `ucscsdk` carries four domain-ish attributes on
`LsServer`: `domain`, `domain_dn`, `domain_group` and `domain_group_dn`.
**`domain` is the one that names the UCS Manager a profile lives on** —
it is the value a UCS Central client opens its second, per-domain session
against, which is the same hop `app.domain.models.manager`'s docstring
records as the one real hierarchy Cisco UCS has. Worth keeping written
down because picking `domain_group` instead is a plausible and silent
mistake.

Profiles with an empty `domain` are ignored for pruning purposes: an
unassociated or domain-less profile says nothing about which domains are
worth contacting.

## Adapter interfaces, MACs and fabric attachments

ADR-0009 records the headline finding: `adaptorExtEthIf` (physical
adapter port, present on every discovered server) and `adaptorHostEthIf`
(logical vNIC, exists only once a service profile is associated) were
strictly complementary on UCSPE 4.2 — of 14 servers, **12 had only
ext-eth, 2 had only host-eth, none had both**. Querying either class
alone leaves most of the fleet with no network data at all. Both are
collected, and fabric attachments use both together.

**On real, fully-associated hardware both classes are present at once**
for the same physical port, unlike UCSPE — a physical uplink and the
vNIC UCS Manager virtualizes on top of it can both report the same
`fabric`, so `len(ProviderServer.attachments)` overcounts physical
uplinks if the two aren't told apart. `ProviderAttachment.interface_kind`
is `"PHYSICAL"` for an `adaptorExtEthIf` attachment, `"VNIC"` for an
`adaptorHostEthIf` one — the two are always built as separate
`_attachments()` calls and concatenated, never merged into one pass, so
this label is exact rather than inferred. **To answer "what is this
server physically cabled to," read the `PHYSICAL` rows**; the `VNIC`
rows describe the OS-facing logical carve-out pinned to one fabric side,
which matters for troubleshooting guest networking but not for verifying
the wire.

### Which MAC the OS actually sees

The two classes are *not* interchangeable for this, and the ADRs do not
cover it. `nic_macs` prefers the vNIC's MAC and falls back to the
physical port's only when a server has no vNIC — unassociated, or an
older-generation adapter with no vNIC abstraction — so a cabled server
never reports zero NICs merely for lacking a service profile.

A Cisco VIC presents virtual interfaces to the OS rather than exposing
its physical uplink port, so the physical port's burned-in MAC faces the
fabric interconnect and is never visible inside the OS.

> **Provenance conflict, unresolved — do not treat this as settled.**
> The commit that introduced this preference (`de46ee3`, "feat: prefer
> vNIC MACs over physical-port MACs") states in its message that it was
> "confirmed against a live UCS Central run, where a fully-associated
> server reported one physical and one logical MAC per adapter port, both
> real Cisco OUIs, and only the vNIC MAC matched the OS-visible NICs".
> **No such run is recorded in any ADR**, and ADR-0014's Status still
> reads "Not yet validated against a live UCS Central". The 2026-08-17
> ADR updates were code and documentation changes, not validation runs.
> Either the commit message describes a run nobody wrote up, or it
> overstates its evidence. Resolve this before relying on the claim, and
> record the answer in ADR-0014.

MACs reported as `not applicable` or `derived` are UCS placeholders and
are skipped.

### Operational state mapping

ADR-0009 records that passing UCS's own vocabulary (`operable`,
`admin-down`) through untouched left every fabric path counted as neither
up nor down — a server with four attachments stored `fabric_paths_up: 0,
fabric_paths_down: 0`, silently disabling the connectivity health signal
for every UCS server.

`admin-down` maps to `DISABLED`, not `DOWN`: it is the normal state of an
adapter port on a server with no service profile, and
`compute_connectivity_facts` counts neither, so an unassociated server
does not masquerade as a fault.

### Fabric Interconnect identity

`networkElement` is queried domain-wide — exactly two results in
practice, the redundant FI pair — and joined onto every attachment by
its bare `id` (`"A"`/`"B"`), the same value `switch_id` already carries.
It supplies `fabric_model` and `fabric_serial`, the two identifying
facts UCS Manager's schema actually exposes per Fabric Interconnect.

**`fabric_name` and `fabric_id` remain deliberately unpopulated.**
`ucsmsdk`'s `NetworkElement` has no distinct configured-hostname
property — confirmed from the installed package's `prop_meta`, which
lists `model`/`serial`/`oob_if_ip`/etc. but nothing name-shaped beyond
the `id` letter already captured as `fabric`. UCS's own architecture is
why: a domain's two Fabric Interconnects share one cluster identity
(`topSystem.name`/`topSystem.address`, the management VIP), and each
physical FI otherwise has only its own out-of-band console IP
(`NetworkElement.oob_if_ip`, not currently collected) — there is no
separate per-FI DNS-style hostname in UCS Manager's own data model to
read. A synthetic label (e.g. `f"{domain_name}-{switch_id}"`) was
considered and deliberately not written into `fabric_name`: that field
already flows through `IngestService` into the persisted `Server`
document and API/UI, so inventing a value UCS Manager never configured
would read as more authoritative than it is. Revisit if an operator's
naming convention makes that derivation reliably correct for their
fleet.

`fabric_port` comes from `peer_dn`, which only physical ports
(`adaptorExtEthIf`) carry — logical vNICs have no fabric-side peer. An
interface whose `switch_id` is absent or `NONE` produces no attachment at
all.

`ProviderAttachment.provider` records **which collector observed** the
attachment, not which product owns the fabric: a UCS Central run reports
`UCS_CENTRAL` for hardware still fronted by a domain's own fabric
interconnects.

## CPU, memory and storage

ADR-0009's 2026-08-16 update covers the class hierarchy (`computeBlade`
-> `computeBoard` -> `processorUnit`, and -> `storageController` ->
`storageLocalDisk`), the ancestor-walk join, and the
`MediaType`/`HealthSeverity` mappings.

`ComputeBoard`, `ProcessorUnit`, `StorageController` and
`StorageLocalDisk` were also confirmed to exist as real classes in
`ucscsdk`, property-identical to `ucsmsdk` (ADR-0014's 2026-08-16
update). That confirmation is now historical: the collector reads this
data from each domain's UCS Manager, never from Central's replica.

### Unit assumptions — both unproven

**`total_memory` is assumed to be MB, and this remains unproven.** The
SDK cannot settle it: the package is code-generated from the MIT schema
and carries no unit metadata for any property —
`prop_meta["total_memory"]` is a bare `uint` with no unit annotation, doc
string or range. The assumption rests on UCS Manager's own GUI labelling
the column "Total Memory (MB)". ADR-0009's "Still not settled" records
why UCSPE did not resolve it: it reports `49152` for all 14 servers
regardless of model, and contradicts itself elsewhere. **Only real
hardware will settle this.**

**`storageLocalDisk.size` is assumed to be MB on the same basis, also
unproven.** The one piece of independent corroboration is weak: a sibling
project's from-scratch Cisco collector
(`team-redbull/ServerScanner`) made the identical MB assumption for this
exact field. Still unverified until a disk of known size is read back.

`size == "not-applicable"` is a documented sentinel
(`StorageLocalDiskConsts.SIZE_NOT_APPLICABLE`) and means unknown
capacity, not zero. A disk whose capacity cannot be read still
contributes a drive entry — model, serial and health are worth reporting
— with `capacity_bytes=None`, and adds nothing to the storage total
rather than counting as zero bytes.

### CPU model

`cpu_model` comes from the **first equipped** `processorUnit`. UCS
reports one per socket and a real multi-socket server is expected to be
symmetric, so the first equipped socket represents the server rather than
being an arbitrary pick.

**Caveat, still open.** `cpu_model` and the per-drive storage detail were
mapped from SDK evidence alone (ADR-0009's 2026-08-16 update) and had not
been exercised against a live domain when that update was written. That
is still true: no live UCS Central validation has happened, so nothing
has since confirmed these fields against real hardware.

## SDK behaviour, sessions and timeouts

### `ucsmsdk` (UCS Manager)

ADR-0009 covers the async-wrapper decision: `ucsmsdk` has no async
support, so every `login`/`logout`/`query_classid` is dispatched through
`asyncio.to_thread`, with one `UcsManagerClient` (and `UcsHandle`) per
domain per run, never shared across concurrent tasks.

Confirmed against the installed `ucsmsdk==0.9.27` source, not
documentation:

- **Constructor**: `UcsHandle(ip, username, password, port=None,
  secure=None, proxy=None, timeout=None)`. `timeout` is urllib's, so it
  bounds each individual socket operation (connect, and each blocking
  read). It is **not** a total-request or total-run deadline.
- **Endpoint must be a bare hostname or IP.** `UcsSession.__create_uri`
  builds `"%s://%s:%s" % (protocol, ip, port)` with `ip` interpolated
  raw, so a scheme or an embedded port produces a mangled URL
  (`https://https://host:443`). `_validate_endpoint` rejects both up
  front rather than letting it surface as an opaque connection error.
- **`query_classid` returns a plain list**, never `None`; `[]` when
  empty.
- **Exceptions come from two disjoint trees**, both rooted at
  `Exception`: `UcsError` (with `UcsException`,
  `UcsValidationException`) and `UcsWrapperException` (with
  `UcsLoginError`, `UcsConnectionError`, `UcsOperationError`). Catching
  the two roots covers all six.
- **Network failures are in neither tree.** `ucsdriver.post` re-raises
  urllib's errors untouched, and `URLError`/`socket.timeout` are
  `OSError` subclasses — so every call catches `OSError` alongside the
  SDK roots, and callers only ever see one `UcsManagerConnectionError`.
- **`login()` raises on bad credentials** and never returns a falsy
  value, so an authentication failure cannot silently proceed as if
  connected.
- **`logout()` before a successful login is a no-op** (`_logout` returns
  early when the session cookie is `None`), so calling it from a
  `finally` after a failed login costs nothing and sends no request.

**Why `login()` sits inside the `try`** in `list_servers`:
`ucssession._login` sets the session cookie and only *then* calls
`_update_version()` / `_update_domain_name_and_ip()`, either of which can
raise with the session already established server-side. Logging in
outside the `try` would leak that session until UCS Manager times it out.

### `ucscsdk` (UCS Central)

Confirmed directly against the installed `ucscsdk==0.9.0.10` source,
which `diff -rq` showed byte-identical to github.com/CiscoUcs/ucscsdk
master at `6c9a34f` (ADR-0014).

- **`UcscHandle(ip, username, password, port=443, proxy=None)` takes no
  `timeout`.** `ucscsession.post` calls `ucscdriver.post(uri, data,
  read)` without forwarding one, so `urlopen` runs with `timeout=None`
  and a wedged Central blocks forever. ADR-0014 records this and the
  decision to impose a deadline in the wrapper;
  `UcsCentralClient._with_timeout` is that control.
- **The imposed timeout leaks a thread, deliberately.**
  `asyncio.wait_for` cancels the *await*, not the worker thread — a
  timed-out call leaves its thread blocked in `urlopen` until the OS
  gives up. Acceptable only because a collector run is a short-lived
  CronJob process, with the CronJob's `activeDeadlineSeconds` as the
  outer backstop. The alternative is a collector that hangs until
  Kubernetes kills it with no logged reason. ADR-0014 records the
  tradeoff.
- **`port` must be 443.** `__create_uri` raises for any other value, so
  unlike `ucsmsdk` there is nothing to configure and an endpoint with an
  embedded port is always wrong.
- **`endpoint` must be a bare hostname or IP.** `__create_uri` builds
  `"%s://%s%s%s" % ("https", ip, ":", port)` with `ip` interpolated raw,
  so a scheme produces `https://https://host:443`.
- **`query_classid(class_id=None, filter_str=None, hierarchy=False,
  need_response=False, dme='central-mgr')` returns a list**, `[]` when
  empty.
- **Exceptions come from two disjoint trees**, both rooted at
  `Exception`: `UcscError` (with `UcscException`,
  `UcscValidationException`) and `UcscWrapperException` (with
  `UcscLoginError`, `UcscConnectionError`, `UcscOperationError`).
  Catching the two roots covers all six — the same split `ucsmsdk` uses,
  with a `c` in the names. Recorded here because the class names differ
  and this set is written down nowhere else.
- **Network failures are not in either tree**, and **`logout()` before a
  successful login is a no-op** — both exactly as for `ucsmsdk` above.

### Sessions

A client is one instance per collector run, never pooled or reused:
neither handle is documented as safe for concurrent use from multiple
tasks, and `asyncio.to_thread`'s one-call-at-a-time dispatch from a
single client instance keeps every call to that handle sequential.

`list_servers` must be iterated to exhaustion or closed via
`contextlib.aclosing` — abandoning the generator part-way leaves sessions
to be cleaned up at GC time, and both Central and UCS Manager enforce a
per-user session cap. `IngestService.ingest` drains it fully.

### Debugging

`INVENTORY_UCS_DUMP_XML=1` turns on the SDK's own request/response XML
dump, and is shared by both Cisco clients — one switch for "show me the
Cisco XML", whichever collector is running.
`tools/run_collector.py --debug-xml` sets it. It is read from the
environment rather than threaded through constructors because it belongs
to a run, not to a manager. Never on by default: the dump includes full
inventory payloads and would bury a real collector run.

### Error model

`UcsManagerConnectionError` and `UcsCentralConnectionError` are
deliberately not `app.errors.AppError` subclasses — see
`app.domain.ports.credentials.CredentialNotFoundError` for why
collector-side errors do not go through the API's RFC 9457 error model.

## UCS Central domain discovery and pruning

### Central is a directory, not an inventory source

Central is asked exactly two questions:

1. `computeSystem` — which domains are registered, at what address, and
   what Central believes each holds.
2. `lsServer` — which service-profile names live in which domain, used
   *only* to skip domains that certainly hold nothing of ours.

Everything in a `ProviderServer` then comes from that domain's own UCS
Manager through `UcsManagerProvider` unchanged. The reasoning for that
split, and the removal of the standalone UCS Manager entry point, is
ADR-0014's 2026-08-17 update.

### Why one login reaches every domain

Central hands out each registered domain's address as
`ComputeSystem.address` — confirmed by Cisco's own
`ucscsdk/utils/ucscdomain.py`, whose `get_domain()` filters
`ComputeSystem` on exactly that property (ADR-0014). A single UCS Manager
service account is valid across the domains of one fleet, which is why
there is no `INVENTORY_UCS_MANAGER_IP` and why
`app.infrastructure.credentials.env.resolve_login` exists to ask for a
login without an endpoint.

`DomainTarget.endpoint` falls back to the domain's `name` when `address`
is empty; a domain with neither is skipped with a
`ucs_central.domain_without_address` warning, so the log says "no
address" rather than surfacing an opaque DNS failure later.

### The pruning rules

**Skipping means "do not open a session to this domain". It is never a
deletion**, in UCS or in MongoDB — `IngestService` has no reap path, so a
skipped domain's existing documents simply stop being refreshed.

Three rules, in the order they apply:

1. A domain with no reachable address is skipped.
2. A domain is skipped **only** when Central reports profiles for it
   *and* none match the collector's name pattern.
3. **A domain whose profiles Central does not report is collected, never
   skipped.** ADR-0014's open question is precisely whether Central
   replicates domain-*local* service profiles; pruning on missing
   evidence would silently drop exactly the domains that question is
   about, and the symptom would be a mysteriously small inventory rather
   than an error. Absence of evidence gets a round trip, not a guess.

Rules 2 and 3 are pruning and are silent by design. Rule 1 is a fault:
a registered domain with no address is recorded in the provider's
`collection_errors`, alongside any domain whose collection raised, and
`tools.run_collector` turns a non-empty list into a `PARTIAL` report and
exit status 3. So a domain that *should* have been readable and was not
turns the run red, while a domain that genuinely holds nothing of ours
does not.

The pattern is applied with **`re.search`**, matching
`tools.run_collector._NameFilteredProvider` exactly. This function must
never be stricter than that filter, which is the only thing that decides
which servers are actually ingested. `^ocp` means "starts with" because
the operator wrote the anchor, not because the code added one. The test
`test_pattern_matching_mid_name_still_keeps_the_domain` fails if anyone
changes it to `re.match`.

A profile's `domain` key is looked up against the domain's name, then its
address, then its id: Central may key it by any of the three depending on
how the domain was registered.

### Why no server-side `filter_str`

`ucscsdk` supports
`query_classid(..., filter_str='(name, "ocp.*", type="re")')`, and it is
deliberately unused. The name a server is filtered on lives on its
`lsServer` profile, not on the compute MO, so a server-side filter
narrows only one query — and it would put a second, subtly different copy
of "which servers are mine" beside `_NameFilteredProvider`. One filter,
applied once. The code carries a `# ponytail:` marker saying to revisit
only if payload size ever measurably hurts; at 10k servers this is a few
MB per run.

### `central_external_id` — why DNs are re-rooted

**UCS Manager DNs are domain-local and therefore collide.** Every domain
in the fleet has a `sys/chassis-1/blade-1`. Servers collected here all
carry `manager_id = mgr_ucs_central` (one `Manager` document per type,
from `tools.run_collector.manager_for`), so their external ids land in a
single `Server.external_ids[mgr_ucs_central]` namespace where an
un-rooted DN identifies several machines at once.

Identity resolution happens on vendor+serial in
`app.application.services.ingest`, so this would **not** merge two
servers — it would make the recorded external id useless for saying
*which* server, and `domain_id_from_dn` could no longer recover the
owning domain.

Anything not under `sys/` is returned unchanged: org-rooted DNs
(`org-root/ls-...`, which is what `profile_template_external_id` carries)
are global in Central and already correct.

### Cost, and the shape this deliberately is not

Two Central queries, then per collected domain one login plus the 10
domain-wide queries `UcsManagerProvider` issues (pinned by
`test_scales_query_count_independently_of_fleet_size`). **That per-domain cost
is flat in server count** — a domain holding 500 servers costs the same
as one holding 10 — so the only levers are how many domains get
contacted (pruning) and how many at once (`concurrency`, from
`INVENTORY_UCS_CENTRAL_DOMAIN_CONCURRENCY`).

This is not the shape a naive port takes. The obvious implementation
resolves each matching profile's `pn_dn` with a `query_dn` and then walks
`query_children` for board, CPUs, controllers and disks — five-plus round
trips *per server*, on top of the per-domain login. The
domain-wide-query-plus-client-side-join that `UcsManagerProvider` already
implements collapses that to a constant number of requests per domain,
and `group_by_owning_server_dn` is what makes the join exact.

Per-domain results are buffered into a list rather than streamed through
a fan-in queue: one domain's `ProviderServer` list is small next to the
managed objects `UcsManagerProvider` already holds for that same domain
while joining them, so a queue would add machinery to save nothing.

A failing domain is caught, logged as `ucs_central.domain_failed` with a
`collected_before_failure` count, and contributes an empty list — an
unreachable or slow domain must never cost the fleet its entire run,
mirroring `tools.run_collector._run_one_manager`'s per-manager
isolation.

### `_log_domains` — the check on Central's domain list

The domain list is the one thing this collector still takes from
Central's replica and cannot verify any other way, so every run emits
`ucs_central.domain_summary` per registered domain. `total_physical_cnt`
is what Central *believes* a domain holds; `collected_servers` is what
that domain's own UCS Manager actually returned. Three failures live in
the gap, none visible from the total ingested count:

1. A domain Central lists but whose UCS Manager we could not collect from
   — an unreachable address, a login not valid on that domain, or pruning
   that cut too hard.
2. A domain whose registration Central has but whose inventory it never
   synced — `inventory_status` says so directly.
3. Central's own view going stale — `last_refreshed_ts`. Note this is now
   only ever a statement about the *domain list*, never about server
   data, which comes straight from the domain.

**`collected_servers` is `None` for a domain never contacted and `0` for
one contacted that returned nothing.** That distinction is load-bearing:
"we did not ask" and "we asked and got nothing" are different failures
and must not collapse into the same number.

`ucs_central.domain_collected_nothing` warns on the reported>0 /
collected==0 gap. `ucs_central.profiles_in_unregistered_domain` warns
when Central reports profiles whose `domain` matches no registered
`computeSystem` — inventory we were told about but cannot reach, and the
one case the per-domain loop cannot surface since it iterates registered
domains.


## Intersight managed objects

**Provenance for this whole section: the generated models in the
installed `intersight==1.0.11.2026072720` wheel**, which are the OpenAPI
contract rendered as Python. Nothing here has been confirmed against a
live tenant — see ADR-0017's "Validation" section, which states plainly
that no live Intersight call has ever been made. Facts below are
therefore *contract-verified*, not *fleet-verified*, and that is a weaker
claim than anything in the sections above. Anything a real run settles
should be moved here with its own provenance line.

### The server anchor

`compute.PhysicalSummary` is the consolidated blade-and-rack view and is
the only anchor query. It carries `Moid`, `Dn`, `Name`, `UserLabel`,
`Model`, `Serial`, `Uuid`, `Vendor`, `TotalMemory`, `NumCpus`,
`NumCpuCores`, `NumThreads`, `MgmtIpAddress`, `ManagementMode`,
`ServiceProfile`, `AlarmSummary`, `ChassisId` and `Presence`.

It carries **no** typed relationship lists — `compute.Blade` and
`compute.RackUnit` have `adapters`, `storage_controllers`, `bmc` and so
on, and the summary has none of them. That does *not* mean per-server
queries are needed: every child object carries an **inverse** reference
back up, which is what the fleet-wide join uses.

`ManagementMode` is one of `IntersightStandalone`, `UCSM` or `Intersight`
(`compute_physical_summary.py:471`), and the schema's declared default is
`IntersightStandalone`.

### The join topology

Each sub-resource is listed once for the whole estate and attached
client-side. Two of the joins are two-hop, because the object does not
reference the server directly:

| Object | Reaches its server via |
|---|---|
| `server.Profile` | `AssociatedServer`, else `AssignedServer` |
| `adapter.Unit` | `ComputeBlade` / `ComputeRackUnit` |
| `adapter.ExtEthInterface` | `AdapterUnit` -> `adapter.Unit` |
| `adapter.HostEthInterface` | `AdapterUnit` -> `adapter.Unit` |
| `storage.Controller` | `ComputeBlade` / `ComputeRackUnit` |
| `storage.PhysicalDisk` | `StorageController` -> `storage.Controller` |
| `graphics.Card` | `ComputeBlade` / `ComputeRackUnit` |
| `management.Controller` | `ComputeBlade` / `ComputeRackUnit` |
| `management.Interface` | `ManagementController` -> `management.Controller` |

Exactly one of `ComputeBlade`/`ComputeRackUnit` is set on any given
object, depending on whether the server is a blade or a rack unit.

### The server's name

The same trap UCS Manager has, and the contract is explicit about it.
`compute.PhysicalSummary.Name` is **never** an operator hostname: it is
the fabric-interconnect cluster name plus a chassis/slot when
UCSM-attached, the CIMC's own name in standalone mode, and model plus
chassis/server id under Intersight management. The real name is
`server.Profile.Name`, reached through the *inverse* relationship.

**`server.Profile` has no `Dn` field at all** (verified: absent from its
`attribute_map`). This matters twice — selecting one would risk failing
the whole profiles query, and it means an Intersight server has **no org
path to fall back to** when its name carries no site token. UCS Central's
servers do; these do not, and they resolve to no site. The only DN
available is `ServiceProfile` on the summary, which the contract says is
populated *only* in UCSM mode.

`server.Profile` carries both `AssignedServer` and `AssociatedServer`,
both typed `ComputePhysicalRelationship`, with no documented precedence.
The collector prefers `AssociatedServer` — the machine actually running
the configuration — and falls back to `AssignedServer`.

### PHYSICAL versus VNIC

Settled by the SDK's own docstrings, and it is the inverse-looking pair
of names that makes this worth writing down:

- **`adapter.ExtEthInterface`** is the *physical* cabled uplink. It has
  `SwitchId`, `ExtEthInterfaceId`, `PeerDn`, `PeerPortId`, `MacAddress`.
- **`adapter.HostEthInterface`** is the *vNIC* — its own docstring uses
  the word "vNIC". It has `Name`, `HostEthInterfaceId`, `VnicDn`,
  `MacAddress`, and **no `SwitchId`**.

`vnic.EthIf` is a **design-time policy object** (LAN Connectivity Policy
configuration), not live state. Do not use it for attachments.

Neither interface class carries a numeric speed. Only the switch-side
`ether.PhysicalPort`/`ether.HostPort` have `OperSpeed`/`AdminSpeed`, as
free-form strings of unverified format, so `speed_mbps` is `None`.

### Units — the one that can silently corrupt data

- **`TotalMemory` has NO documented unit**, on `compute.PhysicalSummary`,
  `compute.Blade` or `compute.RackUnit` — all three carry the identical
  unit-less docstring "The total memory available on the server.". Its
  sibling `AvailableMemory` *is* documented "in MB", and per-DIMM
  `memory.Unit.Capacity` is documented "in MiB". The collector assumes
  MiB, matching `ucs_manager.mapping`'s assumption for the same hardware.
  **If that is wrong, memory is over-reported by 4.86% on every server,
  silently.** `tools/verify_intersight.py` section 4 settles it against a
  real server's DIMM sum in one query. **Unresolved as of 2026-08-29.**
- **`storage.PhysicalDisk.Size` and `.RawSize` are documented "in MB"**,
  and are **strings**, needing parsing.
- **`storage.PhysicalDisk.NonCoercedSizeBytes` is documented in bytes**
  and is an int. The collector prefers it precisely because it names its
  own unit, and falls back to `Size` only when it is absent.

### GPUs — a capability ceiling, not a gap

`graphics.Card` carries `Model`, `Pid`, `Vendor`, `Serial`, `PciAddress`,
`GpuId`, `OperState`, `FirmwareVersion`. It carries **no memory,
temperature, power draw, or ECC field**, and neither does `pci.Device`
or `graphics.Controller` — grepped across every model in the wheel. The
collector therefore reports GPU identity with every telemetry field
`None`. Reporting zeros would read as a healthy idle GPU.

The Redfish collector gets this data because it reads `ProcessorMetrics`
and `EnvironmentMetrics` off the BMC directly. Intersight exposes no
equivalent.

### Transport

- Auth is HTTP Signature `hs2019`. The signed header set is
  `(request-target)`, `Host`, `Date`, `Digest` — **not** `(created)`,
  which is what the draft standard and the SDK default to and which
  Intersight rejects. Taken from Cisco's own canonical example, embedded
  in the wheel's `METADATA`.
- The signing algorithm is chosen by key type, not configured: RSA keys
  (API key v2) sign `RSASSA-PKCS1-v1_5`; EC keys (v3) sign ECDSA.
  **Relying on a library default signs RSA-PSS**, which Intersight
  rejects for a v2 key.
- `$top` maxes at 1000 and `$top`/`$skip` is the only paging mechanism —
  there is no continuation token. Nothing documents the result set as
  stable across pages, so the collector orders every query by `Moid`.
  That is our own prudence, not a documented requirement.
- **Cisco publishes no rate limit anywhere reachable**, and the official
  SDK has no retry or backoff for HTTP status codes at all
  (`rest.py` raises on any non-2xx; `Configuration.retries` falls through
  to urllib3's connection-level `Retry(3)`, which does not cover 429).
  The collector implements its own 429 handling, honouring `Retry-After`.
- **No inventory MO has an `Organization` field** — only policy and
  profile MOs do. Organization scoping therefore cannot be used to
  partition inventory between collectors, which is why the UCS Central
  overlap is resolved by `ManagementMode` instead.
- `Results` is `null`, not `[]`, for an empty result set.
- **An error body is a JSON object with `code`, `message`, `messageId`
  and `traceId`.** *Provenance: a live probe against `intersight.com` on
  2026-08-29 with an unregistered key, which returned `code:
  "UnauthorizedOperation"`, `messageId: "iam_apikey_authheader_invalid"`.*
  This is the one Intersight fact in this file confirmed against the
  running service rather than the contract. The client surfaces
  `message` and `traceId` in its own error text; `traceId` is what Cisco
  needs to find a specific request.
- **Region may matter.** Intersight's 401 text asks the operator to
  "verify the API key and associated account region". Nothing here models
  a region; a tenant in a non-default region would presumably need a
  regional hostname in `INVENTORY_INTERSIGHT_IP`. *Untested.*
