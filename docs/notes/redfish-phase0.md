# Redfish collector — Phase 0: what the project already is

Written before any design work. Everything below was read out of the
repository, not recalled. File paths are the citation; where a claim is
an inference rather than something the code states, it says so.

---

## 1. `ProviderServer`, field by field

Defined in `backend/app/domain/ports/provider.py` as a frozen, slotted
dataclass. It is deliberately flatter than the domain `Server`: "already
vendor-normalized, but not yet correlated". The provider boundary is
where vendor parsing happens — *nothing downstream re-parses a vendor
format*.

| Field | Type / default | Where `ucs_manager.mapping` gets it |
|---|---|---|
| `external_id` | `str` (required) | `computeBlade`/`computeRackUnit` **DN**. UCS Central rewrites it to `compute/sys-<domainId>/…` (`ucs_central.provider.central_external_id`) so it is unique across domains. |
| `vendor` | `str` (required) | Hardcoded `"cisco"`. Must be a `Vendor` member (`dell`/`cisco`/`hp`) — `IngestService` raises otherwise. |
| `name` | `str` (required) | `_server_name`: the **associated service profile's** `lsServer.name`; falls back to the MO's own `name` (empty in practice), then the DN. This is the field that carries the site token. |
| `model` | `str \| None` | `server_mo.model` |
| `serial` | `str \| None` | `server_mo.serial` — the de-facto correlation key (see §4). |
| `system_uuid` | `str \| None` | `server_mo.uuid` |
| `nic_macs` | `tuple[str, ...]` = `()` | `_nic_macs`: `adaptorHostEthIf.mac` (vNICs), falling back to `adaptorExtEthIf.mac` only when there is no vNIC at all. Placeholders `"not applicable"`/`"derived"` are dropped. |
| `bmc_address_raw` | `str \| None` | `_bmc_address`: `vnicIpV4PooledAddr`/`StaticAddr.addr` off the profile DN, else `mgmtIf.ext_ip`; emitted as `ipmi://<host>:623`. Sentinels `0.0.0.0`/`none` → `None`. |
| `bmc_mac` | `str \| None` | `mgmtIf.mac` for the interface `ucs_common.bmc_interface` selects. |
| `manager_id` | `str \| None` | The `Manager.id` the run reports under (`mgr_ucs_central`), passed in — not read from the vendor. |
| `profile_dn` | `str \| None` | `lsServer.dn`. **Not persisted** — `IngestService` never reads it; it exists only for the `--dry-run` print. |
| `profile_template_name` | `str \| None` | `lsServer.src_templ_name` |
| `profile_template_external_id` | `str \| None` | `lsServer.oper_src_templ_name` (resolved DN), falling back to a name→DN map. |
| `cpu_sockets` | `int` = 0 | `server_mo.num_of_cpus` via `_as_int` (never raises; 0 on garbage). |
| `cpu_cores` | `int` = 0 | `server_mo.num_of_cores` |
| `cpu_threads` | `int` = 0 | `server_mo.num_of_threads` |
| `cpu_model` | `str \| None` | First **equipped** `processorUnit.model`. |
| `memory_total_bytes` | `int` = 0 | `server_mo.total_memory` × 1024² — the MB unit is still an **unproven assumption** (ADR-0009, "Still not settled"). |
| `storage_total_bytes` | `int` = 0 | Sum of equipped `storageLocalDisk.size` × 1024². A disk whose size is unreadable contributes `None` capacity and adds **nothing** rather than counting as zero. |
| `storage_drives` | `tuple[dict[str, object], ...]` = `()` | Per-disk dicts: `id`, `model`, `serial`, `media_type` (a `MediaType` value), `capacity_bytes`, `health` (a `HealthSeverity` value). Untyped dicts by design; `ingest._drive_from_dict` coerces. |
| `attachments` | `tuple[ProviderAttachment, ...]` = `()` | Two passes over `adaptorExtEthIf` (`interface_kind="PHYSICAL"`) and `adaptorHostEthIf` (`"VNIC"`). |
| `tags` | `tuple[str, ...]` = `()` | Always empty from UCS. |

`ProviderAttachment` (same file): `type`, `provider`, `fabric`,
`fabric_name`, `fabric_id`, `fabric_model`, `fabric_serial`,
`server_interface`, `server_port`, `fabric_port`, `admin_state`,
`oper_state`, `speed_mbps`, `interface_kind` (default `"PHYSICAL"`).

**`admin_state`/`oper_state` are not free text.** ADR-0009's validation
found the original code passing UCS's own vocabulary straight through,
which made `compute_connectivity_facts` count zero paths up *and* zero
down — silently disabling the connectivity health signal for the entire
Cisco fleet. They must be mapped to `UP`/`DOWN`/`DISABLED`/`UNKNOWN` and
`ENABLED`/`DISABLED`/`UNKNOWN`. This is the single most instructive bug
in the repo for a new collector: a wrong *enum value* is invisible.

### Fields `ProviderServer` does not have

No GPU, no PSU, no per-NIC detail (`NetworkInfo.interfaces` is always
`[]` — `ingest.py` says so explicitly), no memory modules, no power, no
site, no manager *type*. A Redfish collector that wants to report any of
those needs the port extended, which is a design finding to raise, not an
implementation detail.

---

## 2. The `ServerInventoryProvider` contract

```python
class ServerInventoryProvider(Protocol):
    provider_type: str
    async def health_check(self) -> None: ...
    def list_servers(self) -> AsyncIterator[ProviderServer]: ...
```

Three members, and a set of obligations the Protocol cannot express but
the callers enforce:

- **`provider_type`** is a plain class attribute, stored on every server
  as `Server.source_provider`. UCS uses `ManagerType.<X>.value`.
- **`health_check()`** returns `None` on success and raises on failure.
  `IngestService.ingest` calls it as its very first step, *before*
  iterating. `run_collector._run_one_manager` deliberately does **not**
  call it separately — there is a test asserting that
  (`test_does_not_health_check_separately_from_ingest`), because a UCS
  login is ~4 round trips and burns a session slot.
- **`list_servers()`** is declared as a plain method returning an
  `AsyncIterator`, and implemented as an `async def … yield` generator.
  It must be **iterated to exhaustion or closed** — `UcsManagerProvider`
  puts its `logout()` in a `finally`, so abandoning the generator defers
  session teardown to GC. `IngestService` drains it; `UcsCentralProvider`
  wraps each nested iteration in `contextlib.aclosing`.
- **Errors**: a provider may raise from `health_check()` or from
  `list_servers()`. Anything raised out of the ingest loop kills that
  manager's whole run; `run_collector._run_one_manager` catches broadly,
  logs `collector.manager_failed`, and returns exit 1.
- **Per-server errors are the pipeline's problem, not the provider's.**
  `IngestService.ingest` wraps each `_ingest_one` in `try/except`, logs
  `ingest.server_failed`, and increments `IngestSummary.errors`.
- **Optional, read reflectively: `collection_errors: tuple[str, ...]`.**
  Not on the Protocol — `run_collector.collection_errors_of` uses
  `getattr(provider, "collection_errors", ())`, documented as "only a
  collector that fans out over several endpoints can partially fail". A
  non-empty tuple makes the CLI exit **3** (PARTIAL), not 0. **A Redfish
  collector fanning out over N BMCs is exactly the shape this exists
  for.** It must be reset at the start of each iteration —
  `UcsCentralProvider.list_servers` clears it first so a second run does
  not inherit the first's failures.
- **What a provider may not do**: declare a server's site (there is no
  `site_id` field, by design), write to MongoDB, talk to the API, or
  apply the name filter (that is `_NameFilteredProvider`'s job, and
  putting it inside the provider would make `--dry-run` lie).

---

## 3. How configuration reaches a collector today

### The shape

One endpoint + one login **per `ManagerType`**, from environment
variables only. `README.md`: "No `Manager` document to create, no secret
volume to mount."

Declared in `backend/app/config/settings.py` as flat `str` fields on a
pydantic-settings `BaseSettings` with `env_prefix="INVENTORY_"`, so
`ucs_central_ip` ⇒ `INVENTORY_UCS_CENTRAL_IP`. Every one defaults to
`""`. Current set, verbatim:

```
INVENTORY_UCS_MANAGER_USERNAME     INVENTORY_UCS_MANAGER_PASSWORD
INVENTORY_UCS_CENTRAL_IP           INVENTORY_UCS_CENTRAL_USERNAME    INVENTORY_UCS_CENTRAL_PASSWORD
INVENTORY_ONEVIEW_IP               INVENTORY_ONEVIEW_USERNAME        INVENTORY_ONEVIEW_PASSWORD
INVENTORY_OME_IP                   INVENTORY_OME_USERNAME            INVENTORY_OME_PASSWORD
INVENTORY_INTERSIGHT_IP            INVENTORY_INTERSIGHT_USERNAME     INVENTORY_INTERSIGHT_PASSWORD
INVENTORY_COLLECTOR_CONNECT_TIMEOUT_SECONDS   (float, default 15.0)
INVENTORY_COLLECTOR_NAME_PATTERN              (regex, `^ocp` in the example env file; "" = everything)
INVENTORY_UCS_CENTRAL_DOMAIN_CONCURRENCY      (int, default 4)
```

Note `INVENTORY_UCS_MANAGER_IP` **does not exist** — a deliberate,
heavily documented carve-out.

### How it is resolved

`app/infrastructure/credentials/env.py` holds **two** maps, not one:

- `_LOGIN_FIELDS: dict[ManagerType, tuple[str, str]]` — username/password
  settings-field names.
- `_ENDPOINT_FIELD: dict[ManagerType, str]` — the address field.

They are separate specifically so "has a login but no endpoint"
(UCS_MANAGER) is a fact of the type rather than a runtime surprise. The
mappings are explicit, not derived from the enum member name, so an enum
rename is a type error rather than a silent config break.

`EnvConnectionResolver.resolve(manager_type)` strips whitespace, collects
every empty value, and raises

```
ManagerNotConfiguredError: "<TYPE> is not configured — set INVENTORY_X, INVENTORY_Y."
```

It **never** returns a partially-populated `ManagerConnection`, because
"a blank password reaches the vendor as a real login attempt and fails as
'bad credentials' rather than as the configuration error it actually is."
`resolve_login(settings, type)` is the login-only variant for UCS
Manager.

`ManagerConnection` is a frozen dataclass `(endpoint, username, password)`
with a **custom `__repr__` that prints `password='***'`** so a traceback
frame or debugger session can never leak it.

### Where it is rejected

`run_collector._run` resolves *before* anything else, and — for
UCS_CENTRAL — pre-flights the second login too, so a half-configured
deployment exits **2** with the variable names printed, rather than
reaching the generic "FAILED (see logs)" exit 1. The provider factory
raises the same error later as a backstop.

### How it arrives in Kubernetes

`deploy/helm/server-inventory/templates/collector-credentials-secret.yaml`
renders **one Secret** with `INVENTORY_*` keys from `.Values.collectors.*`.
Each CronJob pulls it whole with `envFrom: secretRef` — a Secret rather
than inline `env` precisely because `kubectl get cronjob -o yaml` shows
plain `env` values to anyone who can read workloads in the namespace.
Setting `collectors.existingSecret` skips the template entirely so Vault
/ External Secrets / sealed-secrets can own it.

`ucs-central-collector-cronjob.yaml` is the template to mirror:
`concurrencyPolicy: Forbid`, `backoffLimit: 1`,
`activeDeadlineSeconds: 1800`, the API image with an overridden
`command: ["python3","-m","tools.run_collector","--manager-type","UCS_CENTRAL"]`,
`envFrom` the API ConfigMap + the collector Secret, Mongo/Redis URIs via
`secretKeyRef`, and a hardened `securityContext`
(`readOnlyRootFilesystem: true`, `runAsNonRoot: true`, all capabilities
dropped, no privilege escalation).

**`readOnlyRootFilesystem: true` matters for Redfish**: anything wanting
to write a CA bundle, a token cache or a temp file needs an explicit
`emptyDir`, not an assumption.

---

## 4. Site derivation, and a nameless server

`app/domain/value_objects/site.py`. `parse_site_code(name)`:

1. Lowercase, strip, split on `[-_.]+`.
2. Collect tokens that **exactly** equal a `SiteCode` member value
   (`one`/`two`/`three`/`four`/`five`).
3. Exactly one distinct match → that site. Zero or ≥2 → `None`.

Substring matching is explicitly rejected: `ocp4-stone-01` contains
"one" and names no site.

`IngestService._build_server` calls it on `ps.name` and nothing else.
There is no override, no provider input, no config. The stated reason
(README and the module docstring): *"a misconfigured manager cannot
mislabel everything it collects"* — the label is self-correcting; rename
the host and the platform agrees on the next collection.

**A name with no site token yields `site_id=None`**, which is a real,
surfaced state ("Unassigned" in the UI), never a default. It is not an
error and does not fail ingestion. ADR-0009 records exactly this
happening to 10 of 14 UCSPE servers, and calls it correct behaviour.

This is the sharpest constraint on a standalone-BMC collector: a BMC's
identity is an IP or a management hostname, which is very unlikely to
carry `ocp4-…-five-…`. Whatever the provider reports as `name` decides
site, classification, **and** whether `INVENTORY_COLLECTOR_NAME_PATTERN`
(`^ocp`) lets the server in at all. Flagged for Phase 2 — I do not yet
know the right answer and will not guess.

### Identity / correlation, since it bears on the same question

`IngestService._ingest_one` correlates on **`(vendor, serial_normalized)`
only**, and only when a serial is present; with no serial, every run
creates a new document. The full ladder in `Identity`'s docstring
(system_uuid → BMC MAC → NIC MACs → per-manager external_id) is not
implemented. Two unique partial indexes back this at the DB layer
(`uniq_system_uuid`, `uniq_vendor_serial`), and `DuplicateKeyError` is
caught and retried as an update.

So for a standalone server: **`SerialNumber` is the identity in
practice**, `UUID` is a uniqueness constraint that will reject a
collision, and the BMC IP is not an identity at all.

### What happens to a server not seen in a run

I looked for this specifically. **Nothing.** `last_seen_at` is written on
every ingest, and is indexed, sortable and displayed — but no health
policy, no metric resolver (`health/facts.py`), and no pruning job reads
it. There is no tombstoning and no delete path. A partial collection
therefore leaves untouched documents exactly as they were, with a stale
`last_seen_at`. Good news for a fan-out collector; it also means "this
host went away" is currently invisible.

---

## 5. Failure, retry, timeout and logging conventions

**Timeouts.** One knob: `INVENTORY_COLLECTOR_CONNECT_TIMEOUT_SECONDS`
(default 15.0), threaded down as `timeout_seconds`. `ucsmsdk` accepts it
as a **per-socket-operation** timeout — explicitly *not* a total-request
or total-run deadline (`UcsManagerClient.__init__` docstring). `ucscsdk`
accepts none at all, so `ucs_central.client` imposes one with
`asyncio.wait_for`, knowingly leaking the worker thread on timeout
because "a collector run is a short-lived CronJob process, with
`activeDeadlineSeconds` as the outer backstop" (ADR-0014). The outer
budget is Kubernetes', not the code's.

**Retries.** There are **none**, anywhere. No backoff, no re-login, no
per-query retry. A failure is recorded and the unit is abandoned.

**Blocking SDKs.** Every synchronous call goes through
`asyncio.to_thread`. One client (and one session) per endpoint per run,
never pooled or shared across concurrent tasks.

**Error taxonomy.** One exception type per vendor client
(`UcsManagerConnectionError`, `UcsCentralConnectionError`), raised for
rejected credentials, an API-level error response, *and* a network
failure alike — the SDK exceptions and `OSError` are both converted at
the client boundary. **The code does not distinguish auth-rejected from
unreachable.** For Redfish that distinction is safety-critical (BMC
account lockout), so this is a place the convention will need extending,
not copying.

**Session teardown.** `logout()` is best-effort and **never raises** — it
catches everything and logs `ucs_manager.logout_failed`. Called from a
`finally` on every path, including `health_check`.

**Failure containment.** Three concentric levels:

1. `UcsCentralProvider._collect_domain` — one domain's failure is caught,
   logged with `collected_before_failure`, appended to
   `collection_errors`, and returns `[]`. The fleet's run continues.
2. `IngestService.ingest` — one server's failure is caught and counted.
3. `run_collector._run_one_manager` — anything else is caught, logged
   `collector.manager_failed`, exit 1.

**Exit codes**: `0` complete, `1` the run failed, `2` not configured
(names the variables), `3` **PARTIAL** — servers were written but the run
did not see the whole fleet. The comment on 3 is the design intent worth
copying: reported as success, a partial run "is indistinguishable from a
healthy run against a smaller estate, which is how a bad credential on
one domain stays invisible for weeks."

**Logging.** `structlog`, configured once by
`configure_logging(level, service_name, environment)`; JSON in
production, console otherwise. Event names are dotted and namespaced by
component: `ucs_central.domain_plan`, `.domain_skipped`,
`.domain_failed`, `.domain_collected`, `.domain_summary`,
`.domain_collected_nothing`, `.no_domains`,
`.profiles_in_unregistered_domain`, `collector.name_filter_applied`,
`collector.partial_run`, `ingest.completed`. Facts go in kwargs, never
f-strings. Two habits worth naming:

- **Log the all-zero case.** `collector.name_filter_applied` logs
  unconditionally because "0 kept, 0 skipped" (wrong endpoint) and
  "0 kept, 900 skipped" (wrong pattern) are otherwise identical.
- **`hint=` on warnings** carries an operator-facing sentence saying what
  to check.

**Secret hygiene.** `_drop_sensitive_keys` in `logging/config.py` strips
any event key named `password`/`token`/`authorization`/`secret`/
`api_key`/`credential`. It is described as "the last line of defense",
not the mechanism. It matches on **key name only** — it will not save a
token embedded in a message string or a URL, which is precisely the
failure mode of raw HTTP tracing. `--debug-xml` sets
`INVENTORY_UCS_DUMP_XML=1` and lets `ucsmsdk` mask its own credentials;
nothing will do that for a hand-rolled HTTP client.

---

## 6. Local verification — the full gate

```bash
scripts/dev-up.sh up                                    # Mongo + Redis (podman/docker)
uv sync --all-groups                                    # first time only, plus the local env file
uv run python -m tools.seed_inventory --count 1000 --seed 42

uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run ty check backend/app tools

# frontend, only if touched
cd frontend && npm run lint && npm run typecheck && npm run test -- --run && npm run build
npm run test:e2e                                        # needs backend + frontend dev server up
```

All four backend commands are separate CI steps
(`.github/workflows/ci.yml`'s `lint` and `test` jobs). CLAUDE.md records
that running `ruff check` without `ruff format --check` has already
shipped a commit that failed CI on formatting alone. `ruff format .` is
the fix — never hand-fix formatting.

Constraints the gate imposes on new code:

- `ruff` line-length 100, `target-version = "py312"`, rules
  `E,F,I,UP,B,C4,SIM,RUF,ASYNC,S` — **`S` is bandit**, so `verify=False`,
  hardcoded-looking strings and HTTP calls without a timeout will all be
  flagged and need a justified `# noqa` or a different design.
- `ty` against **Python 3.12** (the floor, not CI's 3.13). It replaced
  `mypy --strict` in ADR-0019 and needs neither a plugin for pydantic
  nor an `ignore_missing_imports` override for an untyped third-party
  library: it resolves such a library from its installed source rather
  than giving up on it. The `ucsmsdk.*`/`ucscsdk.*` overrides that used
  to be required are gone.
- pytest markers `unit` / `integration`; `asyncio_mode = "auto"`;
  `pythonpath = ["backend", "."]`.
- Dependencies are pinned to **exact** versions, chosen to match "what
  this platform's actual air-gapped mirror carries" — not the newest
  release. Any new runtime dependency must be checked against that
  constraint, and `requirements.txt` + `pylock.toml` regenerated.

Known local gotcha (not a code bug): rootless Podman containers get
reaped between shell commands when the user session has no systemd
linger. If `dev-up.sh up` succeeds but Mongo is unreachable afterwards,
check `podman ps` before assuming a regression.

---

## 7. Things I noticed that Phase 1/2 will have to answer

Recorded now, not resolved:

1. **Name → site is a hard dependency, and a BMC does not know its
   hostname.** It knows `HostName` on an `EthernetInterface`, sometimes.
   Whatever we choose here also decides whether `^ocp` admits the server
   at all. This is the second-hardest problem after the fleet list.
2. **`Vendor` is a closed enum of `dell`/`cisco`/`hp`.** Redfish
   `Manufacturer` returns free text — `"Dell Inc."`, `"HPE"`,
   `"Lenovo"`, `"Supermicro"`. Lenovo XCC and Supermicro are named in the
   task brief and **have no `Vendor` member**. Ingesting one raises and
   counts as an error. Either the enum changes (a domain decision, and
   the enum's docstring argues hard against an `UNKNOWN`) or those
   machines are out of scope. This is a design finding for Gate 1/2, not
   something to paper over.
3. **`bmc_address_raw` already has Redfish forms.** `bmc_address.py`
   documents `idrac-virtualmedia://…/redfish/v1/Systems/System.Embedded.1`
   and `redfish-virtualmedia://…/redfish/v1/Systems/1`, kept in a
   Metal3-`BareMetalHost`-compatible form. A Redfish collector knows the
   real `@odata.id`, so it can populate this properly rather than
   guessing. Note `_DEFAULT_PORTS` deliberately does *not* default
   Redfish schemes to 443.
4. **`Manager.bmc_credential_ref` is already reserved for exactly this** —
   "talking to a BMC directly (redfish/IPMI, for power actions) is a
   separate concern from querying a manager, with a different blast
   radius, and will need its own credentials when that lands. Kept as a
   name rather than a value — no plaintext secret ever belongs in a
   document." Read before designing credential storage.
5. **No retry, and no auth-vs-unreachable distinction, exists to copy.**
   BMC lockout makes both mandatory here.
6. **`collection_errors` + exit 3 is the partial-run mechanism**, and it
   is exactly right for N BMCs. But it is currently built one string per
   *endpoint*; 40 dead hosts out of 400 means 40 lines printed by
   `run_collector`. Worth thinking about.
7. **Air-gapped, exact-pinned dependencies.** Any Redfish client library
   must be justified against the mirror, and `sushy` in particular drags
   in OpenStack `oslo.*`. Evaluated properly in Phase 1.

---

## 8. Gate 0 decisions (from the user, 2026-08-22)

The three flags in §7 are resolved. Recorded here as given; the
implementation consequences of each are worked in Phase 1/2.

### 8.1 `Vendor` gains a `STANDALONE` member

> "in the vendor for right now lets add a new enum called standalone"

So `Vendor` becomes `dell` / `cisco` / `hp` / `standalone`. This unblocks
Lenovo XCC, Supermicro and OpenBMC whiteboxes, which have no member today
and would otherwise raise in `IngestService._ingest_one` and land in
`IngestSummary.errors`.

**This is not free, and it is a design finding I owe the user rather than
a quiet edit** — the brief says nothing in the API, engines or frontend
should need to change, and the frontend does:

- `frontend/src/types/server.ts:18` — `export type Vendor = "dell" | "cisco" | "hp"`
- `frontend/src/api/sites.ts:45` — `VENDORS` const
- `frontend/src/features/classification/RuleEditorPage.tsx:35` — a second `VENDORS` const
- `frontend/src/features/health/PolicyEditorPage.tsx:39` — a third `VENDORS` const
- `frontend/src/features/sites/SitesOverviewPage.tsx:19` — `VENDOR_LABELS`

Five lines across four files. The **backend** needs only the enum member:
`api/v1/sites.py:56` derives `_VENDOR_ORDER` from `Vendor` itself, and
every API schema types the field as the enum.

The `Vendor` docstring's own argument against an `UNKNOWN` member does
**not** apply here: `standalone` is still "a property of which collector
produced the record", known by construction, not a value guessed from a
payload. That distinction is what makes this consistent with the existing
design rather than a hole in it.

**One open refinement for Gate 1.** Two readings of the instruction:

- **(a) Blanket** — every Redfish-collected server is `standalone`.
- **(b) Fallback** — map `Manufacturer` onto `dell`/`cisco`/`hp` when it
  is recognizably one of them, and use `standalone` only for the rest.

(b) is strictly less lossy: it keeps vendor-scoped classification rules
and health policies (`scope.vendor`) targeting Dell/HPE/Cisco machines,
and keeps the site overview's vendor mix answering "who made these"
rather than "how many are unmanaged". The cost of (a) that (b) avoids is
that with (a) a Dell rack server reads as vendor `standalone` and no
Dell-scoped policy can ever match it. I recommend (b) and will ask at
Gate 1 rather than assume.

### 8.2 `INVENTORY_COLLECTOR_NAME_PATTERN` does not apply to this collector

> "in redfish we dont need the … `^ocp` filter because we already only
> searching for specific bmc so there is already the filter"

Correct, and it follows from the collector's own shape. The name pattern
exists because "a vendor manager holds the whole datacenter, and the name
is the only thing distinguishing this platform's fleet"
(`settings.collector_name_pattern`). A standalone Redfish collector has
no such manager: it talks only to the BMCs the operator explicitly put in
the inventory, so **the inventory list is the collection filter**, and it
is a far more precise one than a regex over a name the BMC may not even
know.

Applying `^ocp` on top would be actively harmful, not merely redundant:
§4 establishes that a BMC's reported name is unlikely to carry `ocp4-…`
at all, so the pattern would silently discard every host the operator
deliberately listed — the "0 kept, 900 skipped" failure that
`collector.name_filter_applied` exists to make visible.

Implementation consequence: `tools/run_collector.py` currently applies
`_filtered(provider, settings.collector_name_pattern)` **unconditionally**
in both `_run_one_manager` and `_dry_run_one_manager`. It has to stop
doing that for this manager type. Cheapest honest form is to pass an
empty pattern for `REDFISH_STANDALONE` at the two call sites in `_run`,
with the reason stated once — not to add a general per-provider opt-out
mechanism for a single case.

This does **not** touch the classification engine: classification runs
inside `IngestService` and still decides UPI vs hosted over what *is*
collected. Only the collection filter is dropped.

### 8.3 `ProviderServer` gains an optional GPU field

> "also the provider server should have the optional GPU"

The domain side already exists and is unused: `Hardware.gpus:
list[Gpu]`, with `Gpu(vendor, model, serial, memory_bytes, health,
pci_address, firmware_version)` in
`backend/app/domain/models/hardware.py:54`. Today
`IngestService._build_server` hardcodes `gpus=[]` with a comment saying
`ProviderServer` has no GPU field "until the port grows one". This grows
it.

Shape mirrors `storage_drives` exactly, for the same reason it was done
that way — an untyped dict tuple keeps the port free of a second copy of
the domain model, and `ingest` does the coercion:

```python
gpus: tuple[dict[str, object], ...] = ()
```

plus a `_gpu_from_dict` in `ingest.py` beside `_drive_from_dict`, and
`gpus=[_gpu_from_dict(g) for g in ps.gpus]` in place of `gpus=[]`.

Defaulting to `()` means `FakeProvider` and both UCS collectors need no
change and keep reporting no GPUs — which is what they know today.

**The Redfish property path for this is not yet verified and will not be
guessed.** Phase 1 confirms it against the DMTF schema before a line of
mapping code exists.
