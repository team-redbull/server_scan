# Architecture

## Purpose

A vendor-neutral inventory of record for a heterogeneous, air-gapped
bare-metal fleet: what servers exist, where, managed by what, classified
how, and how healthy — with every seam a real hardware-manager collector
(Dell OpenManage Enterprise, Cisco UCS Manager, Cisco Intersight, HPE
OneView) and a future OpenShift/MCE agent will need already in place, so
none of those integrations require touching the core.

## Layering

```
backend/app/
  domain/            pure business logic — no I/O, no framework imports
  application/       use-case orchestration over domain + ports
  infrastructure/    MongoDB, Redis, logging — implements domain ports
  api/                FastAPI routers — thin, no business logic
  middleware/         request-id + timing
  observability/      Prometheus metrics
```

Dependency direction is strictly inward: `domain` imports nothing from the
other layers; `infrastructure` implements `Protocol`s declared in `domain`.
This is what lets a real vendor collector, when it lands, become a new
`infrastructure/providers/<vendor>` module plus a `domain` port
implementation — not a change to the classification engine, the health
engine, or any route handler.

```
raw provider facts -> normalized server -> classification -> health policy
                                                    -> persisted state -> UI
```

The fake data generator (slice 1) feeds this same pipeline through a
`ServerInventoryProvider` implementation rather than writing MongoDB
documents directly, so the ingestion path is exercised end-to-end before
any real collector exists.

## Request lifecycle

1. `RequestContextMiddleware` assigns/reuses a request id, binds it into
   `structlog`'s contextvars, and logs one structured `request.completed`
   line per request (replacing uvicorn's plain-text access log).
2. Route handlers depend on services, never touch MongoDB/Redis clients
   directly.
3. Any error — expected (`AppError` subclasses) or not — is rendered by
   `app.exception_handlers` as an [RFC 9457](https://www.rfc-editor.org/rfc/rfc9457)
   Problem Details body (`application/problem+json`), extended with a
   stable `code`, the `request_id`, and structured `details`. RFC 9457 was
   chosen over a bespoke envelope because it is the current IETF standard
   for HTTP API errors, not because any prior project used it.

## Persistence

- **MongoDB is the source of truth.** One `AsyncMongoClient` (PyMongo's
  native async driver — not Motor, which entered its deprecation window in
  May 2026) per process, created during FastAPI's `lifespan` with explicit
  pool and timeout settings, never per-request.
- **Redis is an ephemeral cache only**, cache-aside, with every read path
  falling back to MongoDB on any Redis error or timeout — a Redis outage
  degrades latency, never correctness or availability. `/health/ready`
  reflects this: MongoDB unreachable fails readiness (503); Redis
  unreachable is reported as `"degraded"` without failing it.

## Observability

- **Logging**: `structlog`, with stdlib logging (FastAPI/Uvicorn/PyMongo)
  routed through the same `ProcessorFormatter` pipeline so every log line —
  ours and the framework's — shares one JSON schema in production and one
  readable console format in development.
- **Metrics**: `prometheus-client`'s default registry, scraped at
  `/metrics`. `http_requests_total` and `http_request_duration_seconds`
  are recorded for every request; domain-specific counters (cache hit/miss,
  classification timeouts, etc.) are added alongside the engines that need
  them.
- **Health**: `/health/live` (process liveness, no dependency checks) and
  `/health/ready` (MongoDB required, Redis reported but non-blocking) are
  deliberately unversioned — they're consumed by the container
  orchestrator, not API clients, and must not move if `/api/v1` ever
  becomes `/api/v2`.

## Search, pagination, and caching (slice 1)

- **Search** never sends user input to MongoDB as raw regex. A query is
  lowercased, escaped, and matched as an anchored prefix against
  `search_tokens` (`{"$regex": "^" + re.escape(q)}`) — index-assisted via a
  multikey index, and structurally incapable of ReDoS or an unanchored
  collection scan, unlike unescaped or unanchored regex.
  `app.domain.services.search_tokens.build_search_tokens` builds that field
  at ingest time from name/serial/model/vendor/tags/MACs (both colon and
  bare-hex forms, so a PXE-script-style bare MAC and a colon-form MAC find
  the same server).
- **Pagination is keyset (cursor-based), never `skip`/`offset`.** The
  cursor is an opaque, HMAC-signed token
  (`app.domain.services.cursor`) binding the current filter/sort
  combination — changing a filter mid-pagination invalidates the old
  cursor with a clear error instead of silently returning wrong results.
  Verified end-to-end against 1,000 seeded servers: every server is
  returned exactly once across a full paginated walk, in any page size.
- **List responses are a lean projection** (`ServerSummary`), not the
  persistence model: no `hardware` subdocument, since at the platform's
  ~10k-servers-with-headroom target scale, shipping full hardware detail on
  every row of a list response is pure waste. Full detail
  (`ServerDetail`) is fetched per-server on demand. Both are dedicated API
  schemas (`app/api/v1/schemas.py`), not `Server` returned as-is — this
  also keeps the MongoDB `_id` alias from ever leaking into a public
  response.
- **Redis caching is cache-aside** with revision-keyed detail cache
  entries (`si:1:srv:{id}:r{revision}` — a write makes the old key
  unreachable without an explicit delete) and short-TTL list-page entries.
  Every `CacheClient` method catches Redis errors and returns a miss rather
  than raising, so a Redis outage degrades request latency, never
  correctness — verified with an integration test that points at an
  unreachable Redis and confirms `GET /servers` still serves correctly
  from MongoDB.

## Classification engine (slice 2)

- **Resolution never relies on MongoDB's natural document order.**
  `app.domain.services.classification.classify` filters the ruleset to
  scope-matching, enabled rules, then sorts by
  `(priority DESC, specificity DESC, order ASC, id ASC)` — a total order
  computed in Python from data already fetched, so the result is
  byte-identical across processes, restarts, and however Mongo happens to
  have stored the documents. Verified by shuffling the same rule set 50
  times and asserting an identical winner every time.
- **Scope specificity is powers of two** (`site:4 + manager:2 + vendor:1`)
  so a more-specific scope strictly outranks a less-specific one
  regardless of how many dimensions are set — a site-scoped rule always
  beats a global rule at equal priority, never a coincidence of how many
  scope fields happen to be filled in.
- **Conflicts are recorded, never silently resolved by luck.** If two
  rules tied on precedence disagree about the outcome, the winner is still
  deterministic (lowest `id`), but the disagreement is persisted on the
  result (`conflicts[]`) rather than hidden — surfacing a rule authoring
  mistake instead of quietly picking one arbitrarily.
- **Regex only ever runs in Python**, never as a MongoDB `$regex`, via a
  `RegexEngine` port (`app.domain.services.regex_engine.RegexModuleEngine`)
  built on the third-party `regex` module's `timeout=` parameter — stdlib
  `re` has no way to bound match time at all. A pattern is rejected at
  *write* time if it can't clear a canary suite of pathological inputs
  within the timeout budget; a pattern that still times out during
  evaluation is skipped and counted, never allowed to stall an entire
  classification run. Empirically, `regex`'s backtracking resists classic
  ReDoS shapes far longer than stdlib `re` does (a `(a+)+$`-style pattern
  needs a ~1000+ character subject to blow up here, not ~40) — the canary
  inputs are sized accordingly; see `regex_engine.py`'s docstring.
- **Preview never sends the draft pattern to Mongo either.** It narrows
  candidates by the parts of `scope` Mongo *can* index (vendor, site_id),
  then evaluates the pattern in Python against the capped candidate set —
  same engine, same safety guarantee as the real classifier.

## Health policy engine (slice 3)

- **The override problem** — a site policy must be able to *replace* a
  global default, not just add another alert beside it, while unrelated
  policies keep firing independently — is solved by `policy_key` families
  (`app.domain.models.health_policy`, `app.domain.services.health.
  evaluate.resolve_families`). Policies sharing a `policy_key` compete for
  exactly one winner (highest specificity, then priority); different keys
  are fully independent and all evaluate. A disabled, high-precedence
  family member is how a scope switches a default off entirely — the
  family contributes nothing rather than falling through to the next
  member. Verified live: creating a `GLOBAL_CUSTOM` policy with the same
  `policy_key` as a `SYSTEM_DEFAULT` WARNING policy but `severity: INFO`
  and a higher priority flipped a real seeded server's health from
  WARNING to INFO with zero code change — the platform spec's own
  "changing a threshold must flip the evaluation" requirement, proven
  against live data, not just a unit test.
- **The metric registry is code, not data**
  (`app.domain.services.health.metrics.MetricRegistry`) specifically so an
  operator/metric-type mismatch (`GT` against a list, `IN` with a
  non-list) is rejected when a policy is *saved*, never discovered
  mid-evaluation across the fleet. Extensible per module — a future vendor
  package registers its own metrics at import time; `register()` raises
  loudly on a name collision rather than silently shadowing.
- **Conditions are a closed declarative grammar**
  (`app.domain.services.health.conditions.Condition`) — `all_of`/`any_of`/
  `not`/leaf nodes only, depth- and size-capped, validated against the
  registry before evaluation. No `eval`, no `exec`, no stored code of any
  kind.
- **Message templates are rendered by explicit substitution**, never
  `str.format(**evidence)` — `str.format`'s field syntax reaches attribute
  and index access (`{obj.__class__}`, `{obj[0]}`) even on a template that
  was already validated to reject them, so *validating* the template isn't
  the enforcement, *never calling `.format()` on it at all* is
  (`app.domain.services.health.template`).
- **Preview answers "would this actually fire", not "does the condition
  match in isolation"** — a low-precedence draft can still be shadowed by
  an existing family member for a given server, so `HealthPolicyService.
  preview` splices the draft into the full active policy set and re-runs
  real resolution per candidate. Documented as materially more expensive
  than the classification preview (no Mongo-native condition compilation
  exists yet), bounded by the same scope-filtered, capped candidate scan.

## Ingestion wires both engines together (slice 2 + 3 integration)

`app.application.services.ingest.IngestService` classifies and
health-evaluates a server in the *same* upsert that ingests it — not a
second write — when `classification_service`/`health_service` are
supplied (both optional, defaulting to `None`, so ingestion still works
before either engine exists and tests of the pipeline in isolation don't
need to construct them). `POST /servers/{id}/reclassify` and
`POST /servers/{id}/health/recalculate` re-run the same two engines
on demand, for "I edited a rule/policy, show me the effect on this server
now" without waiting for the next ingest cycle. `app.application.services.
bootstrap` seeds the platform spec's own acceptance-scenario rules and
policies (`^ocp-.*`/`^upi-.*` system defaults, Dell vendor overrides, the
Cisco one-path-down/two-paths-down fabric policies) idempotently at
startup — "seed only if missing, by name" specifically so an admin's edit
to a system default's `enabled` flag survives every restart rather than
being silently re-armed.

Verified against a live 1,000-server seeded dataset: 400 classified
`HOSTED_CLUSTER`, 404 `UPI`, 196 `UNCLASSIFIED` (all via the correct
rule); the exact Cisco fabric acceptance scenario (one path down →
WARNING, two paths down → CRITICAL) reproduced on real seeded servers,
not just fixtures.

## What's implemented vs. planned

**Slice 0**: configuration, error model, logging, request context,
Mongo/Redis lifecycle, health probes, metrics, the container image, and
the local dev stack.

**Slice 1**: the `Server`/`Site`/`Manager` domain model and value objects
(MAC normalization across colon/dash/Cisco-dotted/bare-hex forms; BMC
address parsing for the `idrac-virtualmedia://`/`redfish-virtualmedia://`/
`ipmi://` forms vendors actually report), the provider/repository port
contracts, MongoDB repositories and indexes, Redis caching, a deterministic
seeded fake-data generator feeding a real `IngestService` pipeline (not a
shortcut that writes documents directly), and the `GET /api/v1/servers` +
`GET /api/v1/servers/{id}` API with search/filter/sort/cursor pagination —
plus the matching frontend inventory table and server detail page,
including a Connectivity tab that renders a variable number of fabric
groups rather than assuming exactly two.

**Slices 2 and 3**: the classification engine and rules API, and the
health policy engine and policies API (see above), wired into ingestion
and exposed via reclassify/recalculate endpoints.

**Slice 4**: maintenance and an immutable audit trail.

- `PUT`/`DELETE /api/v1/servers/{id}/maintenance` enable/disable a
  server's maintenance window (`app.application.services.
  maintenance_service.MaintenanceService`) — deliberately touching only
  `Server.maintenance`, never `classification`/`health`, so a server can
  be simultaneously HOSTED_CLUSTER, CRITICAL, and in maintenance without
  the three concepts interfering.
- `audit_events` is append-only by construction, not by convention:
  `MongoAuditEventRepository` exposes only `record()` — no `update`/
  `delete` method exists on the class at all, so no code path in this
  codebase *can* alter or remove a recorded event. `AuditService.record()`
  is the one place every mutation (classification rule CRUD, health
  policy CRUD, maintenance changes, and real classification/health
  transitions from `reclassify`/`recalculate`/ingestion) goes through.
- Ingestion emits `SERVER_CREATED` for genuinely new servers and
  `CLASSIFICATION_CHANGED`/`HEALTH_STATUS_CHANGED` only on a real
  transition — never a generic "updated" event, since ingestion touches
  `last_seen_at` on every server on every run and a naive audit-on-every-
  write would be pure noise with no signal.
- `GET /api/v1/events` and `GET /api/v1/servers/{id}/events` use a
  simpler, unsigned keyset cursor than `servers`' HMAC-signed one — the
  sort order here never varies (`created_at DESC, _id DESC`), and a
  forged/stale cursor on a read-only log has no consequence worse than
  seeing the wrong page. Finding and fixing the cursor was itself the
  most instructive bug this slice produced: every repository in this
  codebase stores `datetime` fields as ISO 8601 *strings* (`model_dump(...,
  mode="json")`), and the first cursor implementation compared a parsed
  Python `datetime` against that stored string in a MongoDB `$lt` query —
  a cross-BSON-type comparison that silently returns wrong results rather
  than raising. See `docs/adr/0006-audit-event-cursor-string-dates.md`.
- `Server.profile_template` — the reusable deployment/configuration
  template a server's profile was provisioned from: UCS Manager's Service
  Profile Template, Intersight's Server Profile Template, HPE OneView's
  Server Profile Template, or a Dell OME Deployment Template. Vendor-
  neutral (`name` + opaque `external_id`), landed alongside slice 4
  because it touches the same `ProviderServer` → `Server` ingestion path.

334 backend tests (unit/integration/api) and the full frontend
lint/typecheck/test/build pipeline pass.

**Slice 5**: the classification-rule and health-policy admin UIs
(`frontend/src/features/classification/`, `frontend/src/features/
health/`) — backend untouched this slice.

- Both editors share one shape: a form for the rule/policy fields, a
  debounced live preview (`PreviewPanel`, hitting the backend's real
  preview endpoints — no client-side re-implementation of resolution
  logic), and a `HistoryPanel` reading the slice-4 audit trail filtered
  to the entity being edited. `source` selection drives which single
  scope field (`site_id`/`manager_type`/`vendor`, or none) is shown and
  required, and the priority-band hint — mirroring
  `validate_rule_write`/`HealthPolicy._priority_within_band` exactly
  (`PRIORITY_BANDS`, `requiredScopeField` in both editors).
- A system (seeded) rule or policy renders locked: every field disabled
  except `enabled`, matching that the backend only permits an enable/
  disable update to a `system: true` record.
- The health-policy editor's `ConditionBuilder` is an MVP visual builder
  (one leaf, or one level of `all_of`/`any_of`) over the closed condition
  grammar, with a JSON "Advanced" escape hatch for anything deeper — it
  never hand-rolls its own validation, the preview endpoint is the source
  of truth for whether a condition is well-formed.
- `ShadowPanel` is the UI surfacing of the `policy_key` shadowing
  mechanism (ADR-0005): given the draft's `policy_key`, it lists sibling
  policies from the same family (excluding the policy being edited) so an
  author isn't blind to what they're about to shadow or be shadowed by —
  a pure client-side derivation over `GET /health-policies`, no dedicated
  backend endpoint, and no client-side re-implementation of the
  precedence resolution itself.
- The dev-only Vite proxy had a real route collision fixed this slice: a
  plain `"/health"` prefix match was swallowing the SPA's own
  `/health-policies` client routes and sending them to the backend's
  liveness endpoints, breaking a hard refresh on those pages. Fixed by
  anchoring the proxy key to a regex (`"^/health/"`), verified live
  against the running dev stack (`GET /health-policies` and
  `/health-policies/new` now serve `index.html`; `/health/live` still
  proxies correctly).
- Verified live against the running dev stack (not just the unit/
  component test suite): classification-rule and health-policy preview
  endpoints called directly and via the SPA's dev proxy return real
  results from the seeded 1,000-server dataset, and backend validation
  errors (out-of-band priority, missing required scope field) surface
  through to the API exactly as the editor's error formatting expects.

**Slice 6**: the 10k/50k performance pass — verifying, against real-scale
data rather than test fixtures, that the platform's stated ~10k-with-
headroom-to-50k target actually holds. See
`docs/adr/0007-scale-verification-and-request-coalescing.md` for the full
writeup; summary:

- `tools/seed_inventory.py` scales linearly (~5.5ms/server through the
  full classify+health-evaluate ingest pipeline) — seeded 50,000 servers
  in the dev database in under 5 minutes.
- `tools/verify_indexes.py` runs `.explain()` for every query shape
  `GET /api/v1/servers` (plus classification/health resolution and
  audit-event reads) can issue, against however many documents are
  currently seeded, and fails if a shape with a supporting index falls
  back to an unexpected `COLLSCAN`. Run against the 50k dataset, it found
  two real index gaps that small-fixture `.explain()` tests could not
  have caught: `last_seen_at`'s index was missing its `_id` tiebreak
  (broke keyset-sortable unfiltered `sort=last_seen_at`), and
  `maintenance.enabled` — a filter whitelisted in `FILTER_FIELDS` — had no
  compound index at all, contradicting `indexes.py`'s own stated design
  rule. Both fixed by adding the missing compound indexes
  (`last_seen_at_id`, `maintenance_enabled_name_id`).
- `tools/loadtest.py` measures real p50/p95/p99 latency under concurrent
  load. It surfaced a cache-stampede tail-latency problem — many
  concurrent identical `GET /api/v1/servers` requests each independently
  missing the 15-second list-page cache and each re-running the same
  expensive query, p99 up to ~4 seconds for a moderately common search
  term. Fixed with `app.infrastructure.singleflight.coalesce`, an
  in-process request-coalescing primitive: concurrent callers for the
  same cache key share one in-flight computation instead of each issuing
  their own. Re-measured p99 for the same scenario: ~156ms.
- A related but distinct tail-latency case — a search term matching zero
  or very few documents forces a full-collection scan since `list_page`'s
  early-stop `limit` never triggers — was found, quantified (~700-800ms
  p99 at 50k), and deliberately left open rather than risk-fixed under
  this slice's time budget; the real fix trades this problem for a
  different one (a blocking in-memory sort that scales with match count)
  whose net direction needs real search-term distribution data this
  platform doesn't have yet. See the ADR's "related, deliberately
  undecided finding" section.
- All of `GET /api/v1/servers`'s filter/sort/search combinations that do
  have a supporting index confirmed IXSCAN (not COLLSCAN) at true 50k
  scale after the fixes above; the unfiltered/`with_count=true` COLLSCANs
  that remain are expected and bounded (a `limit`-capped preview scan, or
  a count the frontend never actually requests).

**Slice 7**: Playwright E2E coverage of the critical admin flows, plus a
real gap it surfaced. See
`docs/adr/0008-e2e-tests-and-maintenance-ui.md` for the full writeup;
summary:

- Maintenance had a fully-built, audited backend (slice 4) but no
  frontend control — `OverviewTab` only ever displayed it read-only,
  since maintenance fell into the gap between slice 1 (inventory) and
  slice 5 (classification/health editors), neither of which owned it.
  Writing an E2E test for "the maintenance flow" is what surfaced there
  was no flow to test. Fixed: `app/api/servers.ts` gained
  `enableMaintenance`/`disableMaintenance`, `app/features/servers/
  hooks.ts` gained the matching mutations, and `OverviewTab` gained an
  inline start/end-maintenance control.
- `frontend/e2e/` (Playwright) covers inventory search/detail/tabs,
  classification-rule create+preview+disable+delete, health-policy
  create+shadow-panel+delete, and maintenance enable/disable — run three
  times back to back with zero leftover test data (`test.afterEach`
  cleanup via direct API calls, keyed by a per-run unique name).
- A real, confirmed Chromium behavior broke the obvious `getByLabel`
  selector approach: a `<label>Text<select>…option…</select></label>`
  field's computed accessible name (and `textContent`) concatenates the
  label text with every option's text, so `getByLabel("Vendor")`
  intermittently matched the *Source* field instead (its option list
  contains `"VENDOR_CUSTOM"`). Fixed with `labeledField()`, an XPath
  `text()`-axis helper that matches only a label's own direct text node.
  Documented as an ADR, not just a code comment, since it will recur the
  moment a new form field is added and a future test reaches for
  `getByLabel` again.
- New `e2e` CI job: real backend + MongoDB + Redis + a small (300-server)
  seeded dataset + the frontend dev server, running the full suite
  headless with `--with-deps` Chromium.

**First real collector**: Cisco UCS Manager — the first vendor
integration that isn't `FakeProvider`. See
`docs/adr/0009-ucs-manager-collector.md` for the full writeup; summary:

- `app.infrastructure.providers.ucs_manager` implements the same
  `ServerInventoryProvider` seam `FakeProvider` already does, over
  Cisco's official `ucsmsdk` Python SDK (synchronous — wrapped in
  `asyncio.to_thread` throughout, since no async UCS SDK exists).
  Identity, hardware summary, service-profile/template resolution, NIC
  MACs, fabric attachments, and CIMC/BMC address are all wired up; CPU
  model string and per-drive storage detail are explicit v1 scope cuts
  (see the ADR), not silent gaps.
- A connection-resolution seam, `app.domain.ports.credentials.
  CredentialResolver`, and its one implementation,
  `EnvConnectionResolver` — one endpoint plus login per `ManagerType`,
  read from settings (`INVENTORY_UCS_CENTRAL_IP`/`_USERNAME`/`_PASSWORD`,
  and the same shape for OneView, OME and Intersight). That
  is the whole of a collector's connection config: no `Manager` document
  to create first, no credentials volume to mount. Resolution is keyed on
  the manager *type*, not a per-manager reference, because this platform
  runs one endpoint per vendor — UCS Manager's multi-domain story is the
  UCS Central collector enumerating its domains at collection time, which
  is also why `UCS_MANAGER` carries a login but no endpoint
  (`INVENTORY_UCS_MANAGER_USERNAME`/`_PASSWORD` only; see
  `docs/adr/0014`'s 2026-08-17 update).
  A half-configured vendor raises `ManagerNotConfiguredError` naming the
  missing variables rather than attempting a login that fails as "bad
  credentials", and `ManagerConnection.__repr__` redacts the password so
  it cannot leak through a traceback.
- `tools/run_collector.py --manager-type UCS_CENTRAL` — the CLI a
  Kubernetes `CronJob` invokes: resolves that type's connection, runs it
  through the same `IngestService` pipeline the fake-data seed script
  uses (classify, health-evaluate, audit, upsert — one write per server),
  and writes back a `Manager` document as a projection of the config so
  the API can resolve `Server.manager_id`. `--dry-run` prints what the
  provider reports and writes nothing; `--debug-xml` dumps every XML
  request/response.
- The Helm chart's `collectors.*` values render into a single `Secret`,
  injected with `envFrom` so passwords are not readable in the pod spec;
  `collectors.existingSecret` defers to a Secret owned by Vault or
  External Secrets instead. The collector shares the API's own container
  image (`Containerfile` also copies `tools/`) rather than building a
  second one. The parallel plain-OpenShift manifest set was removed —
  it duplicated the chart with nothing checking the two agreed, and had
  already drifted; `helm template` covers that case on demand.
- **Validated end to end against a live Cisco UCS Platform Emulator**
  (UCSPE 4.2(2aS9)) — see ADR-0009's validation sections for what that
  proved, disproved and could not settle. Several defects were only
  visible against real hardware: a queried MO class that does not exist
  and aborted every run, a BMC filter that matched nothing, a whole
  class of adapter interface never collected (leaving most servers with
  no MACs or fabric attachments), fabric path counts that were always
  zero because UCS state strings were passed through unmapped, and
  servers named after their chassis slot rather than their service
  profile — which silently defeated both site parsing and
  classification.
- Every `ManagerType` now has a collector except `UCS_MANAGER`, which
  deliberately has no entry point of its own (it is reached through
  `UCS_CENTRAL`); `tools.run_collector` says so in as many words rather
  than claiming a missing feature. Intersight reuses the same three
  settings with different meanings: it signs requests with an API key, so
  `username` is the API Key ID and `password` the secret key. `ONEVIEW` is
  the first collector to populate a server's power supplies — see
  `docs/adr/0022-oneview-only-hpe-collector.md`.

### Standalone Redfish collector (`REDFISH_STANDALONE`)

The second real collector, and the first that points at no manager at
all. It reaches a BMC directly over DMTF Redfish — any conformant one, so
a Cisco CIMC that Intersight cannot yet manage sits alongside an iDRAC
and a current iLO. HPE iLO 4 is excluded on *conformance* grounds rather
than vendor grounds: it answers `/redfish/v1` with pre-Redfish property
spellings, and the collector rejects it at the service root before
sending a credential.

`app.infrastructure.providers.redfish` mirrors `ucs_manager`'s split —
`client.py` (I/O, session lifecycle, TLS, the retry taxonomy),
`mapping.py` (pure payload -> `ProviderServer`), `provider.py` (fan-out
and budgets) — plus `targets.py`, which has no analogue elsewhere because
no other collector has a fleet list to parse. Nothing is shared with
`ucs_common`: that module is DN structure and Cisco SDK presence
semantics, and Redfish has neither.

**Three properties invert what the other collectors assume.**

*The fleet list is input, not discovery.* Nothing enumerates standalone
machines, so a mounted TOML inventory does. It is also the only
collection filter — `INVENTORY_COLLECTOR_NAME_PATTERN` is deliberately
not applied, because a BMC does not know the server's `ocp4-...` name and
`^ocp` would discard every listed host. Credentials resolve
host -> host-named -> group -> defaults -> a fleet-wide fallback, and the
whole file is validated before a single connection opens: an unknown
group, an undefined credential, a duplicate host, an address carrying
credentials, or a TLS opt-out with no written reason each fail the run
naming what to fix. Failing closed matters more than it looks — a
typo'd group name that fell through to the default credential would send
a shared service account to a machine it was never meant for.

*Cost is per server.* A UCS Central run costs ~11 round trips for the
whole Cisco fleet; this costs ~25 against each BMC, on embedded hardware
that degrades when polled. Bounded fleet concurrency, a per-host
wall-clock budget and an in-process run budget are therefore correctness
requirements. The run budget must trip before the CronJob's
`activeDeadlineSeconds`, or the pod is killed with no summary at all.
Hosts are shuffled each run so a truncated sweep does not starve the same
slow hosts forever, and servers stream out as each host finishes rather
than being gathered, so a killed run has already persisted what completed.

*Failure is routine.* Some of several hundred independent BMCs are always
down, so per-host failures accumulate in `collection_errors` (exit 3,
PARTIAL) and the run continues. Only a systemic authentication failure
stops it: a rejected login is never retried, a credential is disabled
after enough *distinct* hosts reject it, and a run-wide failure budget
covers the estate where every BMC has its own account — there the
per-credential counter never trips while accounts lock one at a time.
Both bound damage rather than preventing lockout, and ADR-0016 says so
explicitly rather than over-claiming.

Two guards exist because the collector parses JSON from a device it does
not fully trust: an `@odata.id` that is not a relative path under
`/redfish/v1` is refused rather than followed, and redirects are never
followed — both would retarget the next request, which carries the
session token.

Full design, evidence and open questions: `docs/adr/0016`. Runbook:
`docs/test-redfish-standalone-collector.md`.

### CI supply chain

Every GitHub Action is pinned to a commit SHA rather than a tag, because
a tag can be re-pointed by whoever controls the action's repository and
this repo's `publish` job holds `contents: write` plus a GHCR token. The
release-tagging step is `PaulHatch/semantic-version` (node24) followed by
an explicit `git tag && git push`, with a guard between them that refuses
an empty, duplicate or backwards version — it replaced an action that
declares the now-removed node20 and had no upgrade available.

Nothing updates itself: Dependabot was configured and removed after it
edited `requirements.txt`, a generated air-gap export, as though it were
a source manifest. Keeping the pins current is a documented manual pass
(`CLAUDE.md`'s "Keeping CI current"), and the reasoning behind all of it
is `docs/adr/0013`.

### Staleness detection is the collector's missing half

A CronJob pod lives minutes and exits, so Prometheus never scrapes it —
no metric the Redfish collector emits could report its own *absence*,
which is exactly the failure that matters when hosts quietly stop
answering. The answer is gauges derived from MongoDB's `last_seen_at`
(written on every ingest, currently read by nothing) exported by the API
process. Until that lands, staleness is a documented manual query, and
`docs/test-redfish-standalone-collector.md` §6 carries it rather than
implying coverage that does not exist.

Real authentication is designed (see the session's approved plan) but
lands in a subsequent slice — this document will gain a section and an
ADR once it's implemented, rather than describing not-yet-existing code
as done.

## Further reading

- `docs/arc42.md` — the structured architecture overview (arc42): goals,
  constraints, context, deployment view, quality scenarios, and the
  risk/technical-debt register. It links *into* this document for the
  subsystem detail rather than restating it, so start there for the shape
  of the system and come back here for how a part works.
- `docs/adr/` — architecture decision records, added as decisions are made
  (not written speculatively ahead of the code).
- `deploy/` — OpenShift and Helm deployment manifests.
