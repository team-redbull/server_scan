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
  (service profile) → `lsServer.src_templ_name`, resolved against a
  `lsServiceProfileTemplate` name→dn lookup (built once per collector
  run, not per server) for `ProviderServer.profile_template_external_id`.
  Matches the vendor mapping already documented on `app.domain.models.
  server.ProfileTemplate`.
- CIMC/BMC address: `mgmtIf` child objects (`configResolveChildren`,
  `hierarchy=True` — the exact nesting depth under a server's DN wasn't
  independently confirmed, so the query is hierarchical rather than a
  single-level child fetch), filtered to `access == "out-of-band"`, using
  `ext_ip`. Not every server has one configured; `None` in that case is
  correct data, not a gap.
- NICs and fabric attachments: `adaptorHostEthIf` children — `mac` for
  `nic_macs`, `switch_id` ("A"/"B"/"NONE") for `ProviderAttachment.fabric`.

### Scope cuts, made explicitly rather than silently

- `cpu_model` and `storage_drives`/`storage_total_bytes` stay at their
  zero/`None` defaults — no MO for per-CPU model string or per-drive
  detail was confirmed while building this. `mapping.py`'s module
  docstring tracks this as an open item for the first pass against a real
  domain, not a forgotten field.
- `total_memory`'s unit (MB, converted to bytes) is based on UCS
  Manager's own GUI column label ("Total Memory (MB)"), not an
  independently-fetched XML schema doc — flagged the same way.
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
`login`/`logout`/`query_classid`/`query_children` call through
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
