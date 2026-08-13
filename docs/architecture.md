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
and exposed via reclassify/recalculate endpoints. 302 backend tests
(unit/integration/api) and the full frontend lint/typecheck/test/build
pipeline pass.

Maintenance, audit events, the classification/health-policy editor UIs,
the 10k/50k performance pass, and real authentication are designed (see
the session's approved plan) but land in subsequent slices — this
document will gain a section and an ADR for each as they're implemented,
rather than describing not-yet-existing code as done.

## Further reading

- `docs/adr/` — architecture decision records, added as decisions are made
  (not written speculatively ahead of the code).
- `deploy/` — OpenShift and Helm deployment manifests.
