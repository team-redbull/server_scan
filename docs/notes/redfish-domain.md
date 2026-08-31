# Redfish collector — Phase 1: the problem domain

Companion to `redfish-phase0.md`. Still no design and no code: this is
what the problem *is*, grounded in the repository for §1 and in primary
DMTF/vendor sources for §2 and §3.

Rule held throughout: **no Redfish property path is written here from
memory.** Anything not yet verified against a schema or a real mockup is
marked `UNVERIFIED` and stays that way until it is checked.

---

## 1a. What changes when there is no manager

Every collector this platform has today points at *an aggregator*. UCS
Central is asked what exists; it answers with 152 domains and their
addresses, and the collector fans out from there. The Phase 0 notes
called that shape "one endpoint and one login per manager type", and
`app/infrastructure/credentials/env.py` encodes it as a fact of the type
system.

A standalone Redfish fleet breaks that in six distinct ways. They are
worth separating, because they have different answers.

### 1. There is no API that enumerates the fleet

A BMC knows about *itself*. `GET /redfish/v1/Systems` on one machine
returns that machine (see §2 for the one-vs-many caveat). There is no
query anywhere that returns "every standalone server you own", because
nothing in the estate has that knowledge — that is what *standalone*
means.

**So the list of machines must come from something we own.** This is the
central new problem, and it is not a Redfish problem at all: Redfish
starts working only once you already know an address to point it at.
Every existing collector got its list for free from a vendor product;
this one has to be given one.

Two consequences that are easy to miss:

- **`health_check()` has no obvious subject.** For UCS Central it means
  "can I log into Central". Here there is no single endpoint whose
  reachability means the run can proceed. Checking *every* BMC before
  collecting doubles the login count against devices with small session
  caps — and `IngestService.ingest` calls `health_check()` before
  iterating, so it cannot be a no-op that hides a total misconfiguration
  either. Design decision for Phase 2.
- **An empty run is ambiguous in a new way.** For UCS, "0 servers" means
  a wrong endpoint or a wrong pattern, and
  `collector.name_filter_applied` disambiguates. Here it could also mean
  "the inventory file mounted empty", which is a different fault with a
  different fix.

### 2. Credentials are per-machine, not per-manager

`ManagerConnection` is `(endpoint, username, password)` — one triple,
resolved once per `ManagerType`. That is not a limitation of the
resolver, it is the platform's stated model: `credentials.py` says
resolution is by type, "not by a per-manager reference", and calls that
"a deliberate narrowing".

The real estate is messier than either extreme:

- Some sites run one shared service account across every BMC.
- Some machines have unique credentials (often the ones that were
  onboarded by a different team, or predate the standard).
- Some still have a factory default nobody rotated — which is a finding
  to report, not a credential to rely on.

So the honest requirement is a **resolution chain with precedence**, not
a single pair and not a mandatory per-host entry. Phase 2 designs it.
What matters here is that a design forcing *every* host to carry an
explicit credential is as wrong as one that only allows a global pair:
the first makes a homogeneous 400-host fleet unmaintainable, the second
cannot express the estate that actually exists.

### 3. BMCs are slow, single-purpose embedded devices

This inverts the platform's whole cost model. ADR-0014 records it
plainly for UCS: "one domain costs the same ~11 HTTP round trips whether
it holds 10 servers or 500", and concludes "scale was *not* a factor
either way".

Here the arithmetic is the opposite. Cost is **per server**, and each
server costs *several* round trips (ServiceRoot, session, Systems
collection, the System, then Processors / Memory / Storage / Drives /
EthernetInterfaces — each a separate GET unless `$expand` works, and §3
says it often does not). A 400-host fleet is not 11 requests; it is
several thousand, against hardware whose management processor is
typically a low-clock ARM SoC sharing itself with the web UI, IPMI, KVM
and sensor polling.

Three things follow, all of which the existing collectors could ignore:

- **Bounded parallelism is mandatory, not a tuning knob.**
  `INVENTORY_UCS_CENTRAL_DOMAIN_CONCURRENCY=4` exists to bound *worker
  threads*; the equivalent here exists to avoid melting the estate.
- **A per-host timeout is mandatory**, and the existing
  `INVENTORY_COLLECTOR_CONNECT_TIMEOUT_SECONDS=15.0` is a *per-socket*
  timeout, which is not the same thing as "give up on this host". A host
  that answers every packet slowly can consume unbounded wall-clock
  without ever tripping a socket timeout.
- **A total-run budget is mandatory**, because the CronJob's
  `activeDeadlineSeconds` is a hard kill with no logged reason — ADR-0014
  already names that as the failure mode it was trying to avoid.

Reality check on the platform's stated target: `CLAUDE.md` says ~10,000
servers with headroom to 50,000+. **A standalone Redfish collector at
that scale is a different engineering problem from one at 400.** I am
not going to pretend the design below scales to 10k without saying what
would have to change. Phase 2 states a supported range and its evidence.

### 4. BMC TLS certificates are usually self-signed and often expired

Every BMC ships a self-signed certificate, generated at manufacture or
first boot, commonly with a CN of the serial number or a factory
hostname that matches neither the IP nor the DNS name we reach it by.
Many are past their notAfter date.

The temptation is `verify=False` globally. That is not acceptable in a
production-ready design, and the reason is specific rather than
ceremonial: the collector sends a **plaintext username and password** in
a POST body to establish a session. With verification off, anyone who
can get in the path of that request harvests credentials that, in the
shared-service-account case, are valid on **every BMC in the estate**.
This is the single highest-consequence security decision in the whole
feature.

So verification is the default, and any relaxation is per-host, explicit,
and loudly logged. Ruff's bandit rules will flag a bare `verify=False`
anyway, which is a fair proxy for "this needs a written justification".

### 5. The BMC's identity is not the server's name

A BMC is reachable at an IP or a management hostname —
`10.20.30.41`, `srv-ilo-042.mgmt.example.com`. The platform parses
`site` out of the **server's own name** (`ocp4-prod-tlv-infra-01` →
`one`), and `parse_site_code` requires an exact `-`-delimited token
match.

Phase 0 §4 already established the chain this breaks:

```
provider `name` → parse_site_code → site_id
                → ClassifiableServer.name → classification (UPI/hosted)
```

and, before the Gate 0 decision, also `^ocp` admission. That last link is
now cut (§8.2 of the Phase 0 notes) — which removes the *catastrophic*
version of this problem, where every listed host is silently discarded.
It does not remove the problem: a fleet named after its BMCs is a fleet
with `site_id=None` and `UNCLASSIFIED` everywhere.

What Redfish can offer as a name is genuinely uncertain and is a §2
research item, not a guess:
`ComputerSystem.HostName`, `ComputerSystem.Name`, `Id`, `AssetTag`, and
`EthernetInterface.HostName` are all candidates with different
availability and different meanings. Whether an operator-supplied name
in the inventory file should be allowed to override them is a Phase 2
design question that runs directly into the README's design intent — "a
misconfigured manager cannot mislabel everything it collects" — and I
will not resolve it quietly.

### 6. Failure is normal, not exceptional

With one aggregator, "unreachable" is a single binary event: Central is
up or the run is dead. With 400 independent embedded devices, some
number are *always* down — being reimaged, powered off, on a switch
being upgraded, or genuinely dead. A run where 40 of 400 hosts fail is a
Tuesday, not an incident.

The platform is well-placed for this and I should not over-build:

- `collection_errors` + **exit 3 (PARTIAL)** already exists for exactly
  this shape, and `run_collector` already refuses to report a partial run
  as success.
- **Nothing prunes.** Phase 0 §4 confirmed no code reads `last_seen_at`
  for staleness — no tombstoning, no delete path. So a host that fails to
  answer keeps its document exactly as it was. **A partial run cannot
  make the platform conclude the missing servers vanished.** That is the
  question the brief asked me to check, and the answer is: they are left
  untouched with a stale `last_seen_at`, and nothing surfaces that yet.

The one thing that does not survive contact: `collection_errors` is
currently one message per failed endpoint, printed one per line by
`run_collector`. 40 dead hosts means 40 lines. Phase 2 addresses the
shape of that output.

### 7. New failure mode with no precedent here: account lockout

This has no analogue in any existing collector, so there is no convention
to copy.

Many BMCs lock an account after a small number of failed logins.
Combined with a shared service account, **a retry loop with a wrong
password is a self-inflicted denial of service against the entire
estate's management plane** — and it locks out the humans too, precisely
when they need the BMC to find out what went wrong.

Two hard rules fall out, both of which Phase 2 must encode:

- **Auth failure must fail fast and never retry.** This is the opposite
  of the usual "add a retry for robustness" instinct.
- **"Auth rejected" and "host unreachable" must be distinguishable.**
  Phase 0 §5 found the existing clients deliberately collapse both into
  one exception type. That convention gets extended here, not copied.

A third rule follows from the combination of §1 and §7, and it is worth
stating as a standalone invariant because getting it wrong is the
difference between a collector and an attack tool:

> **A default credential is only ever tried against a host that appears
> in the inventory we were given. It is never tried against an address
> discovered by scanning.**

Sweeping a range and trying a default password against whatever answers
is credential spraying against your own estate, and on BMCs with lockout
policies it will lock accounts across it. If range discovery is
supported at all, discovery and authentication must be separate steps
with separate consent.

---

## 1b. Redfish itself

> Pending — three research agents are gathering primary sources
> (DMTF specification and schema bundle, client libraries and DMTF
> tooling, real-world vendor divergence). This section will carry the
> verified property mapping with a per-property confidence note, and
> nothing will be written here that has not been checked against a cited
> source.

## 1c. Which DMTF repositories are useful, and how

Researched from package metadata and source, not documentation summaries.
**I independently re-verified the claims this section's conclusions rest
on** by reading the fetched source directly — the `verify` handling, the
import-time warning suppression, the response-header logging, the retry
and timeout defaults in `redfish/rest/v1.py`, and sushy's
`connector.py` defaults. Line numbers below are from those files.

The framing is not up for revision by the research: the three validators
validate *a Redfish service*, not our client. They are lab and
characterisation tools. **The CI fixture is the mockup format.** A DMTF
validator will never be a required CI step to "prove our code correct".

### The runtime client: `httpx`, which we already have

Recommendation: **write a small async client on the `httpx==0.28.1`
already pinned in `pyproject.toml`.** Neither candidate library survives
contact with this project's constraints, and for reasons that are
specific rather than aesthetic.

**`DMTF/python-redfish-library` (PyPI `redfish`, 3.3.9) is disqualified
on its TLS default.** In `redfish/rest/v1.py`, inside the request loop:

```python
# TODO: Migration to requests lost the "CA directory" capability; need to revisit
verify = False
if self.cafile:
    verify = self.cafile
```

Verification is **hardcoded off** unless a `cafile` is supplied; there is
no way to say "use the system trust store", and `capath` is accepted and
silently ignored — that is what the TODO is about. Worse, at **module
import time** it calls
`requests.packages.urllib3.disable_warnings(InsecureRequestWarning)`,
suppressing the warning **process-wide for every urllib3 user in the
interpreter**, unconditionally.

For a platform whose entire job is talking to management interfaces
holding root-equivalent credentials, that is not a default to inherit.
Note our ruff `S` gate would **not** catch it: S501 flags `verify=False`
in *our* code, not in a dependency.

Three more, each independently disqualifying-ish:

- **Default `timeout=None`** — blocks forever on a wedged BMC — and
  **`max_retry` defaults to 10**, with the loop being a bare
  `except Exception: … time.sleep(1); continue`. Eleven attempts, flat
  1-second sleep, no backoff. Against a BMC that resets under load that
  is 11× amplification per request.
- **Response headers are logged unredacted** (`v1.py:971`), and the login
  POST's response is exactly where `X-Auth-Token` lives. Requests are
  redacted; responses are not. Debug logging leaks the session token
  verbatim.
- **No `py.typed`**, and **no `types-redfish` on PyPI**. Under
  `mypy --strict` it is an `Any` hole needing an `ignore_missing_imports`
  override plus our own Protocol wrappers — which is most of the code we
  would have written anyway. (ty, which is replacing mypy — ADR-0019 —
  narrows that hole: it type-checks an untyped dependency from its
  installed source, and caught a bad `ucsmsdk` call mypy could not see.)

Its async client is **aiohttp**-based, so "async" means adding a second
compiled HTTP stack (aiohttp + multidict + yarl) beside the httpx already
present. Its `install_requires` is entirely **unpinned** and pulls ~10
distributions including `urllib3[zstd]` → `zstandard` → `cffi`, a
compiled wheel to mirror per platform.

One correction worth keeping, because it narrows a fear rather than
confirming it: **a 401 does not enter that retry loop.** `login()` sees
`resp.status == 401` and raises `InvalidCredentialsError`. Bad
credentials are not retried 11 times. What retries is transport failure —
reset, TLS error, timeout, DNS. The lockout risk is therefore something
*our* design must avoid creating, not something inherited.

**`sushy` (5.13.0) is the better-engineered of the two, and the `oslo.*`
fear was unfounded.** Its runtime `requirements.txt` is four lines —
`pbr`, `requests`, `python-dateutil`, `stevedore` — ~9 transitive
distributions, all pure-Python, *lighter* than the DMTF library. Its
defaults are the ones we want and are worth reading as a specification:

- `verify=True` by default, accepting a CA bundle path, disabling the
  urllib3 warning **only** when you explicitly opt out.
- `default_request_timeout=60` plus a separate `connect_timeout`, forming
  a `(connect, read)` tuple "to allow faster failure on unreachable
  BMCs".
- `tls_min_version` / `tls_ciphers` for BMCs stuck on TLS 1.0/1.1.
- Retries only `_RETRYABLE_EXCEPTIONS`, and **explicitly does not retry
  `SSLError`** — "configuration problems, not transient errors".

It is rejected only because it is **sync-only** (`requests.Session`, so
`asyncio.to_thread` and thread-pool sizing on top of our own concurrency
limit), ships **no `py.typed`**, and models the entire Redfish object
graph — lazy resource classes, field descriptors, OEM dispatch via
`stevedore` entry points, task monitors, power actions, virtual media —
of which a read-only collector uses perhaps 10%, while coupling our
normalization to Ironic's release cadence and its untyped metaclass
machinery. One real cost: `retries = self._server_side_retries or 3`
means **retries cannot be configured to zero**.

**What we would actually have to write** is small and known:
`@odata.id` traversal (one line), the `Members@odata.nextLink` pagination
loop (six lines), session login/logout, the Redfish error-body chain, and
an `OData-Version: 4.0` header. What we would genuinely give up is
**vendor quirk knowledge** — but that is knowledge, not code, and the
valuable parts are already extracted below.

What `httpx` gives that neither does: native async matching our fan-out,
`py.typed` (clean under `--strict`), `verify=` accepting an
`ssl.SSLContext`, timeouts as a first-class `httpx.Timeout(connect=,
read=)`, and **zero new air-gap mirror entries**.

**Carry the knowledge, not the dependency.** These go into the client:

- `Connection: close` on every request. sushy's comment: *"field studies
  reveal that some BMCs choke at long-running persistent HTTP connections
  (or TCP connections)"*. Exactly the class of hard-won fact CLAUDE.md
  says must never be dropped.
- Split timeouts, never `None`.
- `verify=` defaulting to the system trust store; insecure mode an
  explicit, logged, per-host opt-in.
- Configurable minimum TLS version for legacy BMCs.
- Retry transport errors only, bounded, with backoff — **never a 4xx**,
  and never an `SSLError`.
- Session as an `async with` so `logout()` cannot leak. Neither library
  offers this, and BMC concurrent-session caps are small.
- Redact `X-Auth-Token` in **response** logging — the precise place the
  DMTF library leaks it.
- iDRAC's `SYS518` ("not yet ready after previous operation") as a
  known retryable condition.

**Revisit trigger, stated now so it is not a matter of taste later:** if
vendor-specific branches pass ~5, reconsider sushy. It is the only
defensible library of the two and its dependency tree is genuinely
acceptable.

### The CI fixture: the mockup *format*, not the mockup *server*

`DMTF/Redfish-Mockup-Server` is **not on PyPI** — cannot be mirrored or
pinned — was last pushed 2024-06-14, and pulls `grequests` → `gevent` →
`greenlet` (compiled) to serve static JSON. Decisively: **it has no auth
support at all** (its own source TODO says so), so it cannot exercise the
session login/logout path, which is the only genuinely non-trivial part
of the client.

The valuable thing is the **format**: a directory tree of `index.json`
files mirroring URI paths. Serving that is stdlib `http.server` plus a
path-mapping rule, on port 0, in a thread — and that fixture can do what
the DMTF server cannot: reject a request without `X-Auth-Token`, expire a
session mid-run to exercise re-auth, and return a 500 on one collection
member to exercise skip-and-warn. Keep the DMTF server as an optional
local dev tool (digest-pinned container), never as a CI dependency.

**Fixture data.** There is no `DMTF/Redfish-Mockup-Bundle` repo; the
public mockups are listed at `redfish.dmtf.org/redfish/mockups/v1` (23
of them) and distributed as DMTF document **DSP2043**. They are all
DMTF-authored generic mockups — *no Dell/HPE/Cisco captures are
published* — so vendor-shaped fixtures must be captured ourselves.
`Simple Rack-mounted Server with Local Storage`, `Bladed System` and
`Complex Tower Server` map best onto this fleet.

**Open item, honestly flagged:** the DSP2043 bundle's download URL, size
and licence text could not be retrieved — `dmtf.org` returned 403
(Cloudflare) to every attempt. **Its licence must be checked by hand
before committing anything from it.** GitHub also reports `NOASSERTION`
for the DMTF tool repos' licences while the source headers say BSD
3-Clause.

### `Redfish-Mockup-Creator`: output is secret until reviewed

Captures a mockup from a real BMC
(`-A None|Basic|Session`, `--Headers`, `--Time`). Actively maintained.

**It performs no redaction whatsoever** — it dumps whatever the service
returns. Anything captured from real hardware must be scrubbed before it
goes near git:

- `headers.json` under session resources — **`X-Auth-Token`** and
  `Set-Cookie`, verbatim, with `--Headers` + `-A Session`.
- `AccountService/Accounts/*` — real usernames and roles.
- `CertificateService` / `Certificates/*` — subjects and SANs, which
  disclose internal hostnames.
- `ManagerNetworkProtocol`, `EthernetInterfaces` — internal IPs,
  hostnames, VLANs, SNMP communities, LDAP/AD bind DNs.
- `SerialNumber`, `PartNumber`, `UUID`, `AssetTag`,
  `Location.PostalAddress` — real asset IDs and site names.
- `LogServices/*/Entries` — free-text log bodies, the worst offender.

**This project has a specific exposure here.** Its naming convention
encodes the **site code in the server name**, so an unscrubbed capture
publishes datacenter topology. Phase 2 proposes a scrubber that
**whitelists** properties rather than blacklisting them, plus a CI guard
— blacklists fail open, and this is a fail-closed problem.

### The three validators, and what each is actually for

- **`Redfish-Service-Validator`** — validates responses against the CSDL
  schema: resources, properties, types, required-ness, enum values.
  **GET-only, non-destructive.** This is the one that produces citable
  evidence: run it against one real BMC per vendor/model, commit the
  report, and cite its per-property PASS lines as proof that a property
  is present and conformant on that model. That meets CLAUDE.md's
  "verified fact with provenance" bar, and it settles exactly the class
  of question UCSPE could not settle for `total_memory`. Note it depends
  on the DMTF `redfish` library, so it drags that whole tree — fine for a
  lab tool, another reason not to make it a CI step. It takes a local
  schema directory; **confirm it makes no egress to redfish.dmtf.org
  before trusting it air-gapped.**
- **`Redfish-Protocol-Validator`** — HTTP behaviour, not schema: auth
  modes, status codes, required headers, session lifecycle, TLS. Run once
  per vendor as characterisation. **It writes** (creates sessions, tests
  PATCH/POST) — never point it at production BMCs casually.
- **`Redfish-Interop-Validator`** — the interesting one. A *profile* is a
  JSON document declaring, per resource type, what a class of device must
  expose: `ReadRequirement` (Mandatory/Recommended/IfImplemented),
  `MinVersion`, `MinCount`, `ConditionalRequirements`. **This is the
  right formalism for our collector's contract** — a profile stating
  exactly what `ProviderServer` normalization needs lets us validate a
  new vendor's BMC *before* writing collector code. The Redfish analogue
  of `tools/verify_ucs_central.py`. Proposed, not assumed, at Gate 2.

### `Redfish-Tacklebox`: the idioms worth copying

Too heavy to depend on (`redfish`, `XlsxWriter`, `cryptography`,
`pyOpenSSL`), but it is the reference for how experienced clients behave.
It also already uses Google-style docstrings, matching convention 8.

Three distinct defensive patterns, which map directly onto our error
handling:

1. **Whole subsystem absent** → catch, return empty, do not fail the run.
2. **Property present but empty** — guard membership *first*, then
   truthiness. Their comment records a real vendor bug: *"some Chassis
   (such as a secondary Enclosure on HPE Apollo) may have empty PCIe
   devices collections"*. `"Drives" in chassis` and `chassis["Drives"]
   == {}` are different failures.
3. **Individual resource unreachable** → a global workarounds switch that
   downgrades a fetch failure to warn-and-skip instead of aborting, and
   **names the vendor as the culprit in the message**. Strict by default,
   tolerant under a flag. A good model for a fleet collector.

Traversal shape worth stealing: dispatch on
`resource["@odata.type"].rsplit(".")[-1]`; walk **both** direct
collections and the `Links` arrays, because the same physical device
arrives by either route depending on vendor; handle both `Storage`
(modern) and `SimpleStorage` (legacy), skipping the latter when
`Links.Storage` exists; and note `Drive` uses **`PhysicalLocation`**
where everything else uses `Location`.

### Air-gap summary

| Thing | Air-gapped? |
|---|---|
| `httpx==0.28.1` | **Yes** — already mirrored, zero new entries |
| `redfish` | Needs ~10 distributions incl. compiled `zstandard`/`cffi` per platform; all deps unpinned |
| `redfish[aiohttp]` | Adds compiled aiohttp/multidict/yarl per platform |
| `sushy` | Yes — 9 pure-Python distributions. `pbr` makes `setuptools` a runtime dep |
| `Redfish-Mockup-Server` | **No** — not on PyPI. Vendor the file or digest-pin the image |
| Service/Interop validators | Both have local-schema options; **verify no egress before trusting** |
| DMTF published mockups | One-time download then committed — **licence unconfirmed** |

No cloud or KMS dependency appears anywhere in either candidate's tree,
so nothing here is disqualified on the air-gap constraint alone.

## 1d. Real-world vendor divergence

Researched from vendor documentation, upstream sushy/Ironic/Metal3
release notes and real bug reports. Confidence is marked per claim
throughout — `[OK]` primary source, `[WEAK]` single bug report or
community evidence, `[UNVERIFIED]` could not confirm.

**One premise from the task brief did not survive.** The brief (and my
own §1a) assumed vendors return the string `"N/A"` where a schema says
integer. **No primary citation for that was found.** Two promising MAAS
bugs turned out to be something else — one a `None` port in MAAS's own
code, one a truncated JSON file with a handled fallback. What *is*
proven is `null` where a number is expected, and keys absent entirely.
Coding defensively for the string case costs nothing and stays; **citing
it as established does not.** This is exactly the kind of assumption that
became a defect in ADR-0009, so it is recorded rather than quietly kept.

### Baseline: which machines are collectable at all

| Vendor | Usable read-only from | Confidence |
|---|---|---|
| HPE iLO 4 | fw 2.30; **treat < 2.50 as out of scope** | [OK] |
| HPE iLO 5 / 6 | 1.10+ / v1.57+ | [OK] |
| Dell iDRAC 8 / 9 | inventory GETs work well below Metal3's floors | [OK] |
| Lenovo XCC | all generations; XCC3 = Redfish 1.17.0 | [OK] |
| Supermicro | Intel X10 / AMD H11 and later | [OK] |
| OpenBMC | `bmcweb` always present, coverage varies per build | [OK] |

**iLO 4 is effectively a different protocol and should be declared out of
scope.** It reaches "Redfish 1.0 conformance" by mirroring `/rest/v1`,
and returns pre-Redfish spellings unless sent `OData-Version: 4.0`:
`MacAddress` not `MACAddress`, `Power` not `PowerState`,
`AvailableActions` not `Actions`, `@odata.type` in the old dotted form
`ComputerSystem.1.0.0.ComputerSystem` (which breaks any client parsing
the version), OEM namespace `Hp` not `Hpe`, and **no standard `Storage`
at all** — storage exists only under `Oem/Hpe/SmartStorage`. Supporting
it means a second mapping module. Recommendation for Gate 2: exclude it,
say so in the ADR's limits section.

**Licensing** [OK]: Dell states Redfish is included at *all* iDRAC
licence levels — basic inventory GETs are **not** gated (a missing
licence yields 403 or quietly omits properties). HPE: works with free
iLO Standard. **Supermicro is the outlier and the real risk** [WEAK]:
Redfish feature sets reference `SFT-OOB-LIC`/`SFT-DCMS-SINGLE` and the
boundary between free read-only inventory and licensed is not clearly
documented. **Must be tested on a real unlicensed node before promising
Supermicro support.**

### Deviations that change the mapping

- **`Systems` is a collection and "one BMC = one server" does not
  hold.** [OK] OpenBMC explicitly supports multi-host, serving
  `/redfish/v1/Systems/system1/…`. So identity keys on
  `(bmc_address, system @odata.id)`, not on the BMC address. An empty
  `Systems` collection is a real state, not an error. (A plausible
  counter-example was checked and withdrawn: Supermicro Twin/BigTwin
  nodes each have their own BMC.)
- **A listed collection member can 404.** [OK] sushy 5.10.0 had to make
  member-retrieval failures non-fatal because HGX boards advertise
  members that 404. So a member GET failure is *partial data for that
  host*, never a failed host.
- **Ignore `Members@odata.count` entirely and iterate `Members`.**
  [WEAK] Mismatch evidence is OpenBMC-internal only, with none found on
  Dell/HPE/Lenovo — but ignoring the count costs nothing.
- **`CapacityBytes` can be `null`** on empty bays. [OK] sushy 5.11.1
  fixed a `TypeError` on exactly this. The existing UCS convention
  already handles the analogous case correctly: a drive with unreadable
  capacity contributes a drive entry with `capacity_bytes=None` and adds
  **nothing** to the total rather than counting as zero.
- **`MemorySummary.TotalSystemMemoryGiB` is a float and vendors do not
  round it.** [OK] A 768 GB Dell system reports `715.256064`. Spec-
  correct, and it directly affects `ProviderServer.memory_total_bytes`,
  which is an `int`. Rounding is ours to do, and this is the analogue of
  ADR-0009's still-unsettled `total_memory` MB assumption — except here
  the unit is stated by the schema, which is a real improvement.
- **Never use `MemorySummary.Status`/`ProcessorSummary.Status` for
  health.** [OK] Dell deprecated both in iDRAC 6.10.00.00 in favour of
  the individual resources' `Status`. Note it is the *Status sub-object*
  deprecated, not the counts.
- **`ProcessorSummary.CoreCount` may be absent.** [UNVERIFIED] which
  schema version added it, and no per-vendor absence evidence found —
  but the fallback (sum `TotalCores` across the `Processors` collection)
  is needed for older firmware regardless, so design for absence.
- **Properties disappear across major BMC generations, not just
  appear.** [OK] `HttpBootUri` was removed entirely in iDRAC 10; sushy
  5.13.0 had to make it conditional to avoid spurious 400s.
- **Storage is a double-counting trap.** [OK] `SimpleStorage` and
  `Storage` may both be present for the same subsystem, legally. [WEAK]
  newer iLO 5 firmware reports some components twice because the data
  exists under both SmartStorage and standard Storage. [WEAK] Supermicro
  NVMe-only systems return an *empty* standard inventory while the drives
  live at an OEM backplane path. Recommendation: prefer
  `Chassis/*/Drives` as the union source, **dedupe by `@odata.id`**, and
  treat `Storage`/`SimpleStorage` as secondary.

### GPUs: there is no portable standard path

Directly relevant, since Gate 0 added `gpus` to `ProviderServer`.

- [OK] `Processors` with `ProcessorType: "GPU"` is the standard-blessed
  path, and **Lenovo XCC implements it**, with `Links` to
  `PCIeDevice`/`PCIeFunctions`. NVIDIA DGX/HGX expose GPUs through both
  `Processors` and `PCIeDevices`.
- [UNVERIFIED] **No evidence was found that Dell iDRAC, HPE iLO or
  Supermicro reliably populate `Processors` with `ProcessorType: "GPU"`
  for arbitrary add-in GPUs.** On those, `PCIeDevices`/`PCIeFunctions`
  matched on vendor ID is the likelier path, and Dell's real detail sits
  in OEM views.

**Conclusion: GPU inventory is best-effort and must be documented as
such.** Plan a two-source union (`Processors` filtered by
`ProcessorType`, unioned with `PCIeDevices`), deduped — and **do not
promise GPU coverage in the collector's contract.** An empty `gpus`
tuple must mean "not discoverable here", never "this machine has none".

### `$expand` must be probed *and verified*, never trusted

| Vendor / firmware | Advertised | Works |
|---|---|---|
| iLO 4 | — | **"OData query options are not implemented in iLO 4."** [OK] |
| iLO 5 / 6 | yes | yes, but **`Links` is never expanded** (deliberate) [OK] |
| **iLO 7 < 1.22** | **yes** | **`$expand=*` returns HTTP 400** [OK] |
| Lenovo XCC | yes | yes, `MaxLevels: 2` [OK] |
| iDRAC 8 | `ExpandQuery` **empty** | no [WEAK] |
| iDRAC 9 | yes | yes, **not for OEM resources** [WEAK] |
| OpenBMC `bmcweb` | **off by default** | gated behind an "insecure" build flag [OK] |

iLO 7 pre-1.22 is the case that settles the design: it **advertises
support and returns 400**. So `ProtocolFeaturesSupported.ExpandQuery`
is a hint, not a contract — probe it, verify with one real expanded GET,
cache the answer per BMC, and fall back to N+1 GETs. Treating `$expand`
as a latency optimisation with a mandatory fallback is the only safe
design, which matches the brief's own warning.

### Auth: the estate-bricking question

**The critical asymmetry: Dell blocks by source IP; everyone else locks
the account.**

| Vendor | Mechanism | Default | Confidence |
|---|---|---|---|
| Dell iDRAC | **IP blocking** | 3 failures / 60 s → **1-hour block** | [WEAK] — documented for iDRAC 7; the attributes persist on 9 but **its defaults are unconfirmed** |
| HPE iLO | progressive delay | no delay until the 6th failure, then 10 s. **No lockout.** | [OK] |
| Lenovo XCC | **real account lockout** | max failures 0–10; **period `0` = locked until an admin unlocks** | [WEAK] |
| OpenBMC | `AccountLockoutThreshold`/`Duration` | build-dependent; one shipped example 4 / 600 s | [WEAK] |
| Supermicro | none found | — | [UNVERIFIED] |

The consequences differ in kind, not degree:

- **Dell**: one bad credential blocks *the collector host* from every
  iDRAC for an hour, simultaneously. A fleet-wide collection outage,
  recoverable by waiting.
- **Lenovo**: one bad credential locks *the service account*, potentially
  until a human intervenes — **and it locks the humans out too**,
  precisely when they need the BMC.
- **HPE**: merely delayed. Benign.

This confirms §1a.7's rule and sharpens it into three guardrails for
Phase 2:

1. **Never retry a 401.** A 401 is a configuration error, not a transient
   fault.
2. **Circuit-break a credential globally after N 401s across *different*
   BMCs.** N distinct hosts rejecting the same credential means the
   credential is wrong; continuing will lock the estate. This is the one
   genuinely new safety mechanism this collector needs, and it has no
   analogue anywhere in the codebase.
3. **Read `AccountService.AccountLockoutThreshold`/`AccountLockoutDuration`
   where exposed** and record it per BMC.

**Session limits and leaks** [OK]: Supermicro documents 16 concurrent
sessions. Dell and HPE publish no number — HPE admits the limits are
"difficult to know" while warning that exhaustion leads to "embarrassing
situations where server management operations are impossible until…
a manual / physical iLO reset". Sessions **do** leak without an explicit
DELETE; HPE demonstrates them accumulating across repeated logins.
Foreman #37486 ("Maximum sessions limit reached on iDRAC using Redfish")
confirms it happens in production [WEAK — title only, body behind
anti-bot].

**A genuine design question this opens** [OK]: HPE documents that Basic
auth "deletes sessions automatically after HTTP request completion…
creating a very low risk of reaching the maximum number of iLO
sessions." For a collector that makes a burst of GETs and leaves, Basic
eliminates the entire session-leak class. The cost is that **every
request becomes a login event for lockout counting**. The brief asserts
session auth with explicit logout is correct for a batch collector; the
evidence says that is right *if and only if* logout is guaranteed on
every path — which an `async with` gives us. Recorded as a Gate 2
decision with a recommendation, not settled here.

### TLS in practice

Dell ships a self-signed unique certificate, and its **custom signing
certificate** mechanism is the one genuinely scalable answer: import one
signing cert and every iDRAC using it is trusted [OK]. Supermicro is
self-signed and its own docs instruct `curl --insecure` [OK]. HPE and
Lenovo are self-signed-by-default by strong assumption but were not
confirmed to a citation [UNVERIFIED].

Two workable stances, both strictly better than `verify=False`:

- **An internal CA pushed to every BMC.** All four vendors support
  CSR/import. This is the correct long-term answer and is an estate
  policy decision, not a collector feature.
- **TOFU: pin the leaf or SPKI per BMC on first contact, alert on
  change.** About twenty lines, and it detects the interception that
  `verify=False` is blind to.

### Performance: budget hours, not minutes

- [OK] **iLO 4 is genuinely slow**: ~30 iLOs on Gen9, a **3 s timeout was
  insufficient** with failures every 10–15 minutes fleet-wide; **20 s
  fixed it**. Slow endpoints named include `Memory/…`,
  `FirmwareInventory/`, `EthernetInterfaces/`.
- [WEAK] **Old Supermicro BMCs (Redfish v1.8/v1.9) time out under normal
  polling**, and retries=20 with timeout=20 did **not** help; only
  collecting less did. v1.11 on the same estate was fine.
- [WEAK] **`LogServices` is pathological** — scrapes reported at ~10
  minutes. **An inventory collector must not touch it.**
- [WEAK] Polling more often than every 30–60 s degrades BMCs.
- [OK] Sanity anchors from existing tools: `check_redfish` uses 3 retries
  / 7 s; `fishymetrics` uses a 15 s scrape timeout.
- [UNVERIFIED] No per-BMC concurrency limit is published by any vendor,
  and **no evidence that hammering a BMC affects the host OS** — the
  documented harm is to the BMC's own responsiveness and to the other
  management paths sharing it (web UI, virtual console).

**Design implication, and it contradicts the existing default:** budget
**20–30 s per request** on old iLO 4 and old Supermicro, not the
`INVENTORY_COLLECTOR_CONNECT_TIMEOUT_SECONDS=15.0` this repo ships. Go
**wide across BMCs with ~1–2 requests in flight per BMC**. A full-fleet
sweep is an hours-scale job. This is the concrete evidence behind §1a.3's
claim that the platform's cost model inverts here.

### OEM extensions: strictly optional enrichment

`Oem.Dell` (SCP export — an entire configuration in one call),
`Oem.Hpe`/`Oem.Hp` (`SmartStorage`, the *only* storage path on iLO 4),
`Oem.Lenovo` (an extended error registry XCC errors cannot be decoded
without, plus GPU properties on the Processor resource),
`Oem.Supermicro` (the NVMe backplane inventory). OpenBMC has **no stable
OEM namespace** — the shape is a property of the downstream build.

The provider maps from the **standard** schema and treats every OEM field
as optional enrichment. The one place that rule bites is that it makes
iLO 4 storage and Supermicro NVMe-only storage unreachable — which is a
documented limit, not a bug to work around.

### What must be measured on real hardware

Carried forward into the ADR's "still unproven" section:

1. Supermicro's unlicensed-Redfish boundary — the largest single risk to
   claimed vendor coverage.
2. iDRAC 9's actual IP-blocking defaults.
3. iDRAC/iLO concurrent-session ceilings.
4. Whether GPU discovery via `Processors` works at all on Dell, HPE and
   Supermicro.
5. Whether the `"N/A"`-string pattern is real at all.

Items 1, 4 and 5 are settleable in an afternoon by dumping raw JSON from
a handful of each vendor and grepping the numeric fields — the direct
analogue of what UCSPE did for ADR-0009.

## 1b. Redfish itself, and the property mapping

Sourced from the DMTF schema bundle (2026.1) and DSP0266 1.14.0, read as
files rather than as documentation summaries — the same bar ADR-0009 held
`ucsmsdk` to. **I independently re-verified the load-bearing claims**
against the downloaded artifacts: `CoreCount`'s declaring namespace,
`ProcessorSummary`'s "central processors" wording,
`TotalSystemMemoryGiB`'s type, and `Drive.MediaType`'s enum. Each is
quoted below with what it actually says.

`www.dmtf.org` returns HTTP 403 to automated fetches, so the only
readable copy of the specification is **DSP0266 1.14.0** mirrored on
`redfish.dmtf.org`. Section numbers below are from 1.14.0; revisions
1.15+ are `UNVERIFIED`. The *schema* bundle used is current.

### The rule that governs every row below

DSP0266 1.14.0 §9.6.1, verbatim:

> Required properties shall always be returned in a response.
> Properties not returned from a GET operation indicate that the property
> is not supported by the implementation, or by that particular resource
> instance.

**Every resource's `required` list is only `@odata.id`, `@odata.type`,
`Id`, `Name`** — plus `ChassisType` on Chassis, the single non-boilerplate
required property in the entire mapping. Everything the collector wants
is schema-optional. Absence is not an error, and a design that treats it
as one will fail on the first real BMC.

**`null` and absent mean different things**, and §9.6.1 is explicit:

> If an implementation supports a property, it SHALL always provide a
> value for that property. If a value is unknown at the time of the
> operation due to an internal error, or inaccessibility of the data, the
> value of `null` is an acceptable value.

So: **key absent → not supported here** (use the fallback path);
**key present and `null` → supported but unknown this cycle** (transient;
do not overwrite a previously-good value). `payload.get(k)` cannot tell
these apart — a `MISSING` sentinel can. This mirrors how the platform
already treats `site_id=None` as a real state rather than a default.

### A warning that lands directly on this codebase's style

Redfish minor versions are **purely additive**, and enum *members* are
version-gated independently of the properties that carry them —
`ManagerType.Service` (2018.1), `ProcessorType.Partition` (2023.3),
`Status.State.Qualified`/`Degraded` (Resource v1_19_0). OEMs add their own
values on top.

**So no Redfish enum may be parsed with a constructor that raises on an
unknown member.** This repo's own enums (`Vendor`, `SiteCode`,
`MediaType`) are closed *by design* and that stays correct — but they are
*our* domain vocabulary, and every Redfish value must be mapped into them
through a lookup with an `UNKNOWN`/skip fallback, never `OurEnum(value)`
directly. This is precisely the bug class ADR-0009 hit from the other
direction, when UCS's raw `oper_state` vocabulary was passed through
untouched and silently zeroed every fabric-path count.

### The mapping, field by field

Confidence: **[SPEC]** verified in the schema bundle; **[DESIGN]** our
choice, not a Redfish fact; **[OPEN]** unresolved, Gate 1/2 decision.

| `ProviderServer` | Redfish source | Confidence |
|---|---|---|
| `external_id` | `redfish://{host}{system @odata.id}` — composed, not read | [DESIGN] |
| `vendor` | Gate 0: `standalone`, or mapped from `ComputerSystem.Manufacturer` | [OPEN] |
| `name` | `HostName` → `Name` → `Id`, or inventory-supplied | [OPEN] |
| `model` | `ComputerSystem.Model` | [SPEC] v1_0_0 |
| `serial` | `ComputerSystem.SerialNumber` | [SPEC] v1_0_0 |
| `system_uuid` | `ComputerSystem.UUID` | [SPEC] v1_0_0, regex-constrained |
| `nic_macs` | `Systems/{id}/EthernetInterfaces/*` → `MACAddress` ?? `PermanentMACAddress` | [SPEC] v1_0_0 |
| `bmc_address_raw` | composed from the address we connected to + the System's `@odata.id` | [DESIGN] |
| `bmc_mac` | `Links.ManagedBy` → `Managers/{id}/EthernetInterfaces/*` → `PermanentMACAddress` | [SPEC] v1_0_0 |
| `cpu_sockets` | `ProcessorSummary.Count` | [SPEC] v1_0_0 |
| `cpu_cores` | `ProcessorSummary.CoreCount`, else Σ `Processor.TotalCores` | [SPEC] **v1_14_0 = 2020.4** |
| `cpu_threads` | `ProcessorSummary.LogicalProcessorCount`, else Σ `TotalThreads` | [SPEC] v1_5_0 = 2017.3 |
| `cpu_model` | `ProcessorSummary.Model`, else first CPU `Processor.Model` | [SPEC] v1_0_0 |
| `memory_total_bytes` | `MemorySummary.TotalSystemMemoryGiB` × 1024³ | [SPEC] v1_0_0 — **see below** |
| `storage_drives` / `_total_bytes` | follow `Storage.Drives[]` `@odata.id` links | [SPEC] — **see below** |
| `gpus` | `Processors` filtered `ProcessorType == "GPU"` | [SPEC] since Redfish 1.0 |
| `attachments` | **empty tuple** — see below | [DESIGN] |
| `profile_dn`, `profile_template_*` | **`None`** — Redfish has no such concept | [SPEC] |
| `tags` | empty | [DESIGN] |

### The findings that change the code

**1. `ProcessorSummary` counts CPUs only, and excludes GPUs.** Verified
verbatim from `ComputerSystem.v1_28_0.json`:

> `Count`: "shall contain the total number of physical **central**
> processors in the system."
> `CoreCount`: "…total number of **central** processor cores…"

In DMTF's own *Complex Tower Server* mockup the `Processors` collection
reports `Members@odata.count: 10` (CPU1–CPU8, GPU1, GPU2) while
`ProcessorSummary.Count` is `8`. This is normative, not a mockup bug.
**Never derive a GPU count from `ProcessorSummary`, and never assume it
equals the collection length.** It is, however, exactly right for
`cpu_sockets`.

`Processor.SubProcessors` can also expose cores and threads as *nested
Processor resources* with `ProcessorType` `Core`/`Thread` — so filter on
`ProcessorType` before counting anything.

**2. `CoreCount` did not exist before Redfish 2020.4.** Verified: it is
declared in `Namespace="ComputerSystem.v1_14_0"`, whose
`Redfish.Release` annotation is `"2020.4"`. Any 2016–2019 firmware will
not have it, and the fallback (Σ `Processor.TotalCores` over CPUs) is
mandatory, not defensive.

**3. `TotalSystemMemoryGiB` is a `number`, not an `integer`.** Verified:
`type: ["number","null"], units: "GiBy"`. Fractional values are
schema-legal, and §1d found a real Dell reporting `715.256064` for a
768 GB system. `ProviderServer.memory_total_bytes` is an `int`, so the
rounding is ours and must be explicit. Note this is *better* than the UCS
situation: ADR-0009's `total_memory` MB assumption is still unproven,
whereas here the unit is stated normatively by the schema.

**4. `Storage.Drives` is an inline array of links, not a sub-collection.**
This corrects the path in the task brief. There is no
`…/Storage/{id}/Drives` *collection resource*; `Storage.Drives` is
`{"type":"array"}` of `@odata.id` references. Both
`/Systems/{id}/Storage/{id}/Drives/{id}` **and** `/Chassis/{id}/Drives/{id}`
are normative Drive URIs, and DMTF's own mockup uses the *Chassis* form.
**Always follow the `@odata.id`; never construct a drive path.**

**5. Redfish `MediaType` does not have an NVMe member, and ours does.**
Verified: `Drive.MediaType` enum is **exactly `["HDD","SSD","SMR"]`**.
NVMe is expressed through the separate `Protocol` property. Our
`MediaType` is `HDD/SSD/NVME/UNKNOWN`. So the mapping cannot be
value-for-value:

- `MediaType == "SSD"` **and** `Protocol == "NVMe"` → `NVME`
- `MediaType == "SSD"` → `SSD`
- `MediaType == "HDD"` → `HDD`
- `MediaType == "SMR"` → `HDD` (shingled recording is a hard disk;
  `UNKNOWN` would lose real information) — **[DESIGN]**, flagged
- anything else, including absent → `UNKNOWN`

Reading only `MediaType` would report every NVMe drive in the fleet as an
SSD. This is a small, quiet, entirely plausible bug of exactly the shape
ADR-0009 kept finding.

`CapacityBytes` is confirmed in bytes (`units: "By"`) and is
`["integer","null"]` — §1d confirmed real `null`s on empty bays. The
existing UCS convention already handles this correctly and should be
copied verbatim: a drive with unreadable capacity still contributes a
drive entry with `capacity_bytes=None` and adds **nothing** to the total
rather than counting as zero.

**6. Health maps cleanly, three-to-three.** `Status.Health` is exactly
`OK` / `Warning` / `Critical` (all since 1.0) → `HEALTHY` / `WARNING` /
`CRITICAL`, anything else `UNKNOWN`. Separately, `Status.State ==
"Absent"` is the empty-bay/not-installed signal and should skip the
component entirely — the direct analogue of `ucs_common.is_equipped`.
**Do not use `MemorySummary.Status` or `ProcessorSummary.Status`**: §1d
found Dell deprecated both in favour of the individual resources'
`Status`.

**7. GPUs: the standard path exists and is older than expected.**
`ProcessorType == "GPU"` has been valid **since Redfish 1.0** — there is
no version gate. Memory comes from
`Processor.MemorySummary.TotalMemorySizeMiB`, which is **MiB**, not the
GiB used by `ComputerSystem.MemorySummary` — mixing those units is a
1024× error. Added Processor v1_11_0 (2020.4); before that the only path
is Σ `ProcessorMemory[].CapacityMiB` (v1_4_0, 2018.3).

Verified real payload from DMTF's mockup:

```json
{ "Id": "GPU1", "ProcessorType": "GPU",
  "Manufacturer": "Nvidia(R) Corporation", "Model": "Nvidia(R) TU102",
  "MemorySummary": { "TotalMemorySizeMiB": 11264 },
  "TotalCores": 576, "Status": { "State": "Enabled", "Health": "OK" } }
```

The two neighbouring resources are **not** substitutes: `PCIeDevice`
carries slot, link width and serial but **no memory size and nothing
identifying a device as a GPU** (`DeviceType` is `SingleFunction`, not a
device class); `GraphicsController` (2021.1, and its schema never went
past v1_0_2, so it is rare in the field) is the display-controller asset
view and **also has no memory property**. Use `Processors`, optionally
join `Links.PCIeDevice` for slot and serial, treat `GraphicsControllers`
as bonus.

This is the standard's answer. §1d's is harsher: **no evidence Dell, HPE
or Supermicro actually populate it for arbitrary add-in GPUs.** Both are
true — the path is standard *and* vendor support is unproven, which is
why `gpus` ships as best-effort with an empty tuple meaning "not
discoverable here", never "none installed".

**8. `attachments` should be empty, and that is the honest answer.**
`ProviderAttachment` models a fabric attachment — `fabric`, `fabric_port`,
`FABRIC_INTERCONNECT`, the A/B side of a Cisco FI pair. A standalone
server has no fabric interconnect; that is what standalone means. Emitting
NIC link state as a pseudo-attachment would make
`compute_connectivity_facts` produce fabric-path counts for machines with
no fabric, and the two seeded system health policies
(`connectivity.fabric_paths_down_warning`/`_critical`) would evaluate
against fiction.

An empty tuple yields `fabric_paths_total = 0`, so neither policy fires —
correct, and visibly distinct from a Cisco server with a real fault.
**[DESIGN]**, and stated in the ADR rather than left to be discovered.

**9. Pagination: the two research passes disagreed, and both were half
right.** §1d recommended ignoring `Members@odata.count` (weak mismatch
evidence, and ignoring it costs nothing). The schema says something
sharper — §9.6.10 and §7.2.2: the count is the **total number of members
available**, *regardless* of paging. So:

> Iterate `Members`, follow `Members@odata.nextLink` until it is absent,
> and never compare the count to `len(Members)`.

Treating the count as this page's length silently truncates the fleet.
Not following `nextLink` does the same. Both rules are needed.

**10. `$expand` hard-fails with HTTP 501.** §7.3.1: a service *shall*
return 501 for any unsupported query parameter beginning with `$`
(parameters *not* beginning with `$` are silently ignored — different
failure modes). `ProtocolFeaturesSupported` itself only exists from
ServiceRoot v1_3_0 (2017.3); if absent, assume no `$expand`. Combined
with §1d's finding that iLO 7 pre-1.22 *advertises* it and returns 400:
probe, verify with one real expanded GET, cache per BMC, always keep the
N+1 fallback. Two more cases to tolerate: an oversized response returns
**507**, and a per-member failure inside an expanded collection returns
just that member's `@odata.id` — **an expanded collection can still
contain unexpanded link-only members**, and the parser must cope.

**11. A standard concurrency signal exists, and most BMCs won't have
it.** `ServiceRoot.ProtocolFeaturesSupported.MultipleHTTPRequests`
(boolean, ServiceRoot v1_14_0 = 2022.1) "shall indicate whether this
service supports multiple outstanding HTTP requests." **Absent or false →
serialize requests to that BMC.** It is the only standard advertisement
of this, it is recent enough that most fielded hardware lacks it, and
absence must be read conservatively. This feeds directly into the
per-host concurrency design, and it is a much better basis than a guess.
Not to be confused with `Manager.GraphicalConsole.MaxConcurrentSessions`,
which is a KVM limit.

**12. Session auth, exactly.** §13.3.4.2: `POST` to the Sessions
collection with `{"UserName": ..., "Password": ...}`; the response
*shall* carry `X-Auth-Token` (the secret) and `Location` (the session
resource). §13.3.4.4: log out by `DELETE` on that Location. §13.3.4.3:
sessions time out on **inactivity** and carry no expiry timestamp.

**Do not hardcode the Sessions URI** — §13.3.4.1 says find it at
`SessionService.Sessions` *or* `ServiceRoot.Links.Sessions`, and "Both
URIs shall be the same". `ServiceRoot.Links` is required and its own
`required` list is `["Sessions"]`, so it is always present.

And the fact that reopens the brief's assumption — §13.3.1/13.3.3,
verbatim:

> Services … Shall support **both** HTTP Basic authentication and Redfish
> session login authentication.
> Shall **not** require a client that uses HTTP Basic authentication to
> create a session.

So Basic is spec-guaranteed on every conformant service, and it is
stateless: no session to leak, no slot to exhaust, no logout to get wrong
on a crashed run. §1d found HPE documenting exactly that advantage. The
counter-cost is that every request becomes a login event for lockout
counting, which matters given Dell's 3-failures-in-60s IP block. **This
is a genuine Gate 2 decision, and the brief's premise that session auth
is obviously correct for a batch collector does not survive the
evidence.** Recommendation deferred to the design phase with both costs
stated.

**13. Error bodies.** `error.code` and `error.message` are required;
`@Message.ExtendedInfo` is schema-optional but §8.6 says it "should be
present". Client guidance, verbatim: look for `@Message.ExtendedInfo`
first and fall back to `code`/`message`.

### What no amount of schema reading can settle

Everything above is the standard plus DMTF's own mockups. §1d is the
counterweight: real BMCs omit optional properties, expose drives under
one URI family only, and return non-conformant UUID casing. The schema
says what a conformant service *may* do; only hardware says what yours
*does*. That is the same gap ADR-0009 closed with UCSPE, and the same
one this feature has to close with a real BMC and a
`Redfish-Service-Validator` run.

---

## Gate 1 decisions (from the user, 2026-08-22)

### 1. `Vendor`: map the manufacturer first, fall back to `standalone`

> "first try to see if the name there is dell cisco or hp if not use
> standalone"

So option (b) from the Phase 0 notes §8.1. `ComputerSystem.Manufacturer`
is inspected; if it is recognizably Dell, Cisco or HPE the server keeps
that vendor, and only an unrecognized manufacturer becomes `standalone`.

Consequences, and why this is the better half of the choice:

- A Dell rack server collected standalone still reads as `vendor=dell`,
  so vendor-scoped classification rules and health policies
  (`scope.vendor`) can target it, and the site overview's vendor bar keeps
  answering "who made these" rather than "how many are unmanaged".
- `standalone` then carries real meaning: **"a machine whose manufacturer
  this platform does not model"** — Lenovo, Supermicro, whitebox/OpenBMC.
  That is a narrower and more honest claim than "collected without a
  manager".
- `Manufacturer` is `["string","null"]` and **schema-optional**
  (§1b), so an absent or null value is expected, not exceptional, and
  also falls to `standalone`.

**Matching must be deliberate, not a naive substring test.** Real values
include `"Dell Inc."`, `"HPE"`, `"Hewlett Packard Enterprise"`,
`"Cisco Systems Inc."`. A bare `"hp" in manufacturer.lower()` is the same
class of mistake `parse_site_code` explicitly avoids — it would match
inside unrelated words. The mapping will be an explicit, normalized
lookup over known manufacturer forms with `standalone` as the default.
The failure mode is benign in a way site parsing's is not: an unmatched
manufacturer falls back to a correct-but-less-specific vendor, never to a
wrong one.

### 2. HPE iLO 4 is out of scope

> "yes ilo 4 is out of scopes i used the new version of ilo"

Confirmed: the fleet runs current iLO. iLO 4's divergences (§1d) —
`MacAddress` not `MACAddress`, `Power` not `PowerState`,
`AvailableActions` not `Actions`, the old dotted `@odata.type`, OEM
namespace `Hp` not `Hpe`, and **no standard `Storage` at all** — would
require a second mapping module for hardware this estate does not have.

This becomes a stated limit in the ADR, not a silent gap: an iLO 4 host
is expected to fail collection, and it should fail *legibly* rather than
producing a half-populated record. Phase 2 decides whether to detect it
explicitly (the `/redfish/v1` probe can see the old `@odata.type` form)
and report "unsupported firmware" instead of a confusing mapping error.

### 3. Supermicro is out of scope

> "i think that i dont have any supermicro for now lets ignore this and
> assume that i dont have supermicro, in case of problem remember this to
> check"

So the fleet is **Dell iDRAC and current HPE iLO**, with `standalone` as
the vendor for anything else that turns up.

This removes the largest single risk to claimed vendor coverage. The
unresolved `SFT-OOB-LIC` question (§1d) does not need answering, because
no machine in scope depends on it.

**It is recorded rather than deleted, per the user's own instruction, so
that a future symptom is diagnosable.** If a Supermicro board is ever
added to the inventory file, the expected failure is *not* a clean error:
the BMC answers `/redfish/v1` and possibly `/Systems`, then returns
401/403 or empty payloads for the sub-resources carrying CPU, memory and
drive detail. The symptom is a server that ingests with a name and serial
and almost nothing else. **Check licensing before debugging the
collector** — this is the first thing to suspect, and it is exactly the
"succeeds while quietly wrong" shape ADR-0009 kept finding.

The same caveat applies to Lenovo XCC and OpenBMC whiteboxes: in scope
only in the sense that they will map to `standalone` and are entirely
untested. The ADR's limits section says so.

---

## Gate 1 correction: the collector is vendor-agnostic by construction

> "the standalone server doesn't have to be only dell idrac or hpe ilo,
> it could be anything with a BMC that supports Redfish. for example a
> Cisco B300 right now doesn't have support to be managed by Intersight,
> so I would need to talk with its CIMC. and more options — I want to be
> able to talk with each server that has Redfish."

**I over-narrowed at Gate 1 and this corrects it.** I treated "which
vendors are in the fleet" as scoping the implementation. It does not. The
requirement is: **any BMC that speaks conformant Redfish is collectable**,
and the collector must not carry a vendor allowlist.

The mapping in §1b was already built this way — it reads the **standard**
schema and treats OEM namespaces as strictly optional enrichment — so
nothing in the property mapping changes. What changes is what the earlier
"scope" decisions actually mean.

### Restating the three decisions correctly

| Decision | What it is **not** | What it **is** |
|---|---|---|
| Dell + HPE named | Not an allowlist | The vendors we have **evidence** for and can **test** |
| Supermicro "out of scope" | Not blocked in code | **Unowned and untested**; the licensing note stays as a diagnostic |
| iLO 4 excluded | Not a vendor exclusion | A **conformance** exclusion — see below |

**The iLO 4 distinction matters and is worth stating precisely.** It is
excluded because it is *not conformant Redfish*, not because it is HPE.
It returns `MacAddress` instead of `MACAddress`, `Power` instead of
`PowerState`, `AvailableActions` instead of `Actions`, the pre-Redfish
dotted `@odata.type`, and exposes no standard `Storage` at all. A
vendor-agnostic collector that maps from the standard schema will not
understand it — and that is the correct outcome, provided it fails
*legibly*. Any other BMC of any brand that diverges that far is excluded
by the same rule, stated once, with no vendor named.

So the collector's contract is: **conformant Redfish in, `ProviderServer`
out.** Vendor identity is an *output* of what the machine reports, never
an input deciding whether to talk to it.

### Cisco standalone CIMC is a named first-class case

A Cisco server whose CIMC speaks Redfish but which Intersight cannot
manage is exactly the gap this collector exists to close — and it is a
better example than Dell or HPE, because it shows the collector is not a
"cheap vendors" fallback. It is the path for **any** machine no
aggregator owns, including machines from vendors this platform already
has a manager-based collector for.

This is also the case that makes the Cisco entry in `CLAUDE.md` precise
rather than contradictory. There is still exactly one Cisco *manager*
entry point (`UCS_CENTRAL`, per ADR-0014), and that is unchanged: a
domain not registered with Central is uncollectable **through Central**.
`REDFISH_STANDALONE` does not restore a UCS Manager entry point and must
not be described as one — it reaches the machine's BMC directly, over a
different protocol, knowing nothing about domains or service profiles.

### This retroactively justifies the vendor-mapping decision

Phase 0 §8.1 dismissed the double-collection risk too quickly. I wrote
that standalone machines are "by definition not registered with any
manager, so double-collection isn't possible". **The B300 example
disproves that**: it is precisely a machine that is unmanaged *today* and
may be registered with Intersight *later*.

Work it through with the two candidate vendor rules:

- **Manufacturer-mapped (the decision taken).** Collected standalone
  today it is `vendor=cisco` + serial. Collected by a future Intersight
  collector it is `vendor=cisco` + the same serial. `IngestService`
  correlates on `(vendor, serial_normalized)` → **one document, updated**.
  Correct.
- **Blanket `standalone` (the rejected option).** It would be
  `standalone` + serial today and `cisco` + serial later → the correlation
  key differs → **two documents for one physical machine**, and the
  `uniq_vendor_serial` index would not catch it because the vendor
  differs.

So the choice made at Gate 1 is what keeps a machine's identity stable
across the transition from unmanaged to managed. That was not the stated
reason for it, and it is now the strongest one.

**Two residual ambiguities**, noted for Phase 2 rather than solved here:

1. `Server.manager_id` and `source_provider` are single-valued, so a
   machine collected by both paths keeps whichever collector ran last.
   `Identity.external_ids` is a `{manager_id: external_id}` map and holds
   both cleanly, so nothing is lost — but "which manager owns this
   server" becomes last-writer-wins.
2. Nothing prevents an operator listing a BMC in the inventory file for a
   machine that *is* registered with Central. Both collectors would then
   run against it every cycle. Harmless for correctness (they converge on
   one document) but wasteful, and worth a documented warning.

### What this means for the design

- **No vendor allowlist anywhere in the code.** No `if manufacturer ==`.
- **`Manufacturer` decides the reported vendor, never whether to
  collect.**
- **Every property in §1b's mapping is optional and independently
  fallback-guarded**, because "any conformant BMC" means firmware from
  2016 alongside firmware from 2026. §1b's version-to-release table is
  the reference for which fallback is needed where.
- **The ADR's limits section lists vendors by evidence level** — proven,
  expected-but-untested, known-excluded-and-why — rather than as a
  supported-hardware list.

---

## Gate 1 addendum: `sushy-tools` evaluated (user-suggested)

The user pointed at https://github.com/openstack/sushy-tools as a way to
exercise Redfish behaviour. Evaluated **empirically** — the package was
downloaded from PyPI, installed in a clean venv on Python 3.13, run, and
curled. **I re-verified the decisive finding** against the unpacked
sdist.

### It does not solve the auth gap

**There is no `SessionService` anywhere in `sushy-tools`** — not
implemented, and not even declared. Confirmed independently:
`grep -ril "sessionservice\|x-auth-token" sushy_tools/` returns **zero
files**, and `emulator/templates/root.json` contains no `SessionService`
and **no `Links` block at all** (so no `Links.Sessions` either, which
DSP0266 §13.3.4.1 requires and whose own `required` list is
`["Sessions"]`). Live behaviour matched: `POST
/redfish/v1/SessionService/Sessions` returns **404**.

This makes sense once seen: `sushy-tools` exists to test **sushy**, and
sushy authenticates to Ironic's BMCs with **Basic**. Nobody there needed
sessions. So it fails the same requirement DMTF's mockup server fails —
for a different reason.

What it *does* have is **HTTP Basic auth** (`SUSHY_EMULATOR_AUTH_FILE`,
htpasswd, bcrypt digests only), with ServiceRoot correctly excluded from
auth per the spec, and a real 401 with `WWW-Authenticate` on bad
credentials.

### Two more disqualifiers for our specific use

- **The emulator's `ComputerSystem` carries no inventory identity.** No
  `SerialNumber`, no `Model`, no `SKU`, `Manufacturer` hardcoded to
  `"Sushy Emulator"`; and the fake driver does not implement
  `get_total_memory`/`get_total_cpus`, so the Jinja conditionals drop
  `ProcessorSummary.Count` and `MemorySummary.TotalSystemMemoryGiB`
  entirely. It emits exactly what a read-only inventory collector does
  not need (power, boot device, virtual media) and none of what it does.
- **No fault injection.** All 32 `SUSHY_EMULATOR_*` options were
  enumerated; none injects an error or a delay. Of our four required
  failure cases, hand-editing a mockup gets two (a 404 on an advertised
  member, a null-valued property); the 500-on-one-member and the
  slow/hanging response are impossible without patching the handler.

### The genuinely useful outcome: `sushy-static`

`sushy_tools/static/main.py` is **118 lines** (verified) of Apache-2.0
`BaseHTTPRequestHandler` that maps `/redfish/v1/<path>` to
`<mockup>/<path>/index.json`, with optional TLS. **That is precisely the
fixture this project planned to write.**

So the choice is not "build vs. get it free" — it is "vendor 118 lines we
can extend" vs. "take 18 packages (`pbr`, `Flask`, `requests`,
`tenacity`, `bcrypt`, `WebOb` + transitives, ~33 MB, plus a per-arch
compiled `bcrypt` wheel in the air-gap mirror) on a program that still
cannot do the one thing we need."

**Decision: hand-roll, starting from `sushy-static` as the skeleton**
rather than from nothing. Everything missing is small and is exactly the
part we actually need to test:

| Addition | Approx. |
|---|---|
| `SessionService`: POST → 201 + `X-Auth-Token` + `Location`; DELETE drops it | ~20 lines |
| Session expiry (stored timestamp, reject when stale) | ~3 lines |
| Basic auth (`b64decode` + `hmac.compare_digest`) | ~8 lines |
| Fault injection: a `path → (status, delay)` dict at the top of `do_GET` | ~6 lines |
| `ThreadingHTTPServer` so a deliberately-hung request cannot wedge the suite | 1 word |

~100 lines, **zero dependencies, zero air-gap mirror entries**, and full
control over the failure paths that are the whole reason for the tests.

**Where `sushy-tools` would win, and does not here:** if we needed a
*mutable* BMC — power actions, virtual media, boot-order writes — it
would be a clear buy-not-build. This collector is read-only, so that
value is zero.

**Worth taking regardless:** the DMTF mockup bundle as fixture *data*
(DSP2043 — exact URL now known:
`https://www.dmtf.org/sites/default/files/standards/documents/DSP2043_1.0.0.zip`).
Real vendor-shaped JSON, vendored into the repo, no runtime dependency on
anyone. **Its licence still needs a manual check** — `dmtf.org` 403s
automated fetches.

---

## Gate 1 final: vendor is the manufacturer; "standalone" is a provider fact

Settled after the trade-off was worked through concretely. **Two things
that are easy to conflate, decided differently:**

### 1. `Vendor.STANDALONE` is added — as the *unrecognized-manufacturer*
fallback, not as a blanket

Per the earlier Gate 1 decision: read `ComputerSystem.Manufacturer`, map
it onto `dell`/`cisco`/`hp` when it is recognizably one of them, and use
`standalone` **only** when it is not.

- Dell iDRAC collected over Redfish → `vendor=dell`
- Cisco CIMC collected over Redfish → `vendor=cisco`
- HPE iLO collected over Redfish → `vendor=hp`
- Lenovo XCC / whitebox / OpenBMC → `vendor=standalone`
- `Manufacturer` absent or null (schema-optional, §1b) → `vendor=standalone`

So the member's precise meaning is **"a manufacturer this platform does
not model"**, not "collected without a manager". Worth stating plainly in
the enum's docstring, because the name alone suggests the second reading
and a future maintainer will otherwise assume it.

This does not reopen the `Vendor` docstring's argument against an
`UNKNOWN` member. That argument is about never *guessing* a vendor from a
payload; this is a deliberate, documented fallback to a less specific but
still correct value, chosen because the alternative — raising and
counting the server in `IngestSummary.errors` — would drop real machines
out of the inventory entirely.

### 2. "This server has no manager" is carried by `source_provider`

**Not** by `vendor`. The deciding argument is identity stability, worked
through with the user's own B300 example:

| | Option A (`vendor=standalone` for all) | Option B (chosen) |
|---|---|---|
| Collected standalone today | `standalone` + `FCH2201V0AB` | `cisco` + `FCH2201V0AB` |
| Intersight-managed next year | `cisco` + `FCH2201V0AB` | `cisco` + `FCH2201V0AB` |
| Correlation key | **changes** | **stable** |
| Result | **two documents, one machine** | one document, updated |

`IngestService._ingest_one` correlates on
`(identity.vendor, identity.serial_normalized)`. Under Option A the pair
changes when the machine gains a manager, so the platform sees a new
server, and the `uniq_vendor_serial` index cannot catch it because the
vendor genuinely differs. The stale record would sit in the inventory
indefinitely with nothing marking it as a duplicate.

Under Option B the pair is stable, and `source_provider` flips from
`REDFISH_STANDALONE` to `INTERSIGHT` on its own — which is the signal the
user wanted, staying accurate with no maintenance.

**The two dimensions stay orthogonal**, which is what the user's own
"cisco standalone / dell standalone" intuition was reaching for:

- `identity.vendor` — who built the machine
- `source_provider` — how this platform reaches it

Their product is a two-filter query, not an N×M enum:

```
?source_provider=REDFISH_STANDALONE                 every standalone server
?vendor=cisco&source_provider=REDFISH_STANDALONE    "cisco standalone"
?vendor=dell&source_provider=REDFISH_STANDALONE     "dell standalone"
```

### What this costs in code

`source_provider` is already written on every server by `IngestService`
and already returned by the detail API (`schemas.py:127`) — but it is
**not** in `search.FILTER_FIELDS` and **not** on the list response. To
make the queries above real, Phase 3 adds:

1. `"source_provider": "source_provider"` to `FILTER_FIELDS`.
2. A compound index alongside `_id`, matching the convention in
   `mongodb/indexes.py` (every filterable field is index-backed there —
   ADR-0007 found an unindexed whitelisted filter the hard way).
3. `source_provider` on the list-item schema, so the UI can show it
   without a per-row detail fetch.
4. A UI affordance — at minimum a visible marker on the server detail
   page, ideally a filter beside the existing vendor/site selects.

**This is a frontend and API change, and it is deliberate.** The task
brief said nothing outside the provider should need to change; this does,
and it is the user's explicit requirement rather than an implementation
detail I chose. Recorded here and carried into the ADR.

### Authorized (user, 2026-08-23): the API and frontend changes are in scope

> "you can add the filter whitelist, the api and frontend change for the
> source_provider and everything you need to make it work"

So the four items above are approved work, not a finding to raise. They
land in Phase 3 as a distinct, separately-committed unit from the
collector itself — the collector is `feat:` for a new provider; this is a
`feat:` for a new filter dimension, and they are independently reviewable.

Concrete scope, to be confirmed against the code when implemented:

1. `search.FILTER_FIELDS` gains `"source_provider": "source_provider"`.
2. `mongodb/indexes.py` gains a compound `(source_provider, _id)` index.
   Non-optional: every whitelisted filter in this repo is index-backed,
   and ADR-0007 found the one that wasn't by load-testing 50k documents.
3. `api/v1/schemas.py` — `source_provider` onto the **list** item schema
   (it is already on the detail schema at line 127).
4. Frontend — the `Vendor` union gains `"standalone"`, the three
   hardcoded `VENDORS` consts and `VENDOR_LABELS` gain it, and a
   `source_provider` filter joins the existing vendor/site selects on the
   inventory page.

**Test obligation**: an API test asserting `?source_provider=…` filters
correctly and that an unknown value still raises `UnknownFilterError`
(the whitelist's existing contract), plus an index-coverage check
consistent with how ADR-0007 verifies the others.
