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
groups rather than assuming exactly two. 103 backend tests
(unit/integration/api) and the full frontend lint/typecheck/test/build
pipeline pass; verified against a live 1,000-server seeded dataset,
including the spec's own `search=ocp-dell` and Cisco Fabric-A-up/
Fabric-B-down acceptance scenarios.

The classification engine, health policy engine, maintenance, and audit
events are designed (see the session's approved plan) but land in
subsequent slices — this document will gain a section and an ADR for each
as they're implemented, rather than describing not-yet-existing code as
done.

## Further reading

- `docs/adr/` — architecture decision records, added as decisions are made
  (not written speculatively ahead of the code).
- `deploy/` — OpenShift and Helm deployment manifests.
