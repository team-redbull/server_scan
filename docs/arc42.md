# Server Inventory Platform — architecture (arc42)

Follows the [arc42](https://arc42.org) template. Written 2026-08-30
against commit `e570fa8`.

**How this relates to the other documents.** This is the structured
overview: what the system is for, what constrains it, how it is put
together, and what is risky about it. It deliberately does **not**
restate the detail that already lives elsewhere, because a second copy
of a technical explanation is a second copy to keep true:

| For | Read |
|---|---|
| Why a decision was made | `docs/adr/` — 18 records, cited throughout below |
| How a subsystem actually works | `docs/architecture.md` |
| Verified Cisco implementation facts | `docs/cisco-collectors.md` |
| Working in this repo | `CLAUDE.md` |
| Air-gapped mirroring | `docs/air-gap.md` |
| A picture of the runtime | `docs/diagrams/runtime-architecture.html` — open it in a browser |

Sections 4–8 therefore read as maps with pointers, not as prose
duplicates. Where this document is the only place a fact is written
down, it says so.

---

## 1. Introduction and Goals

An **inventory of record for a heterogeneous, air-gapped bare-metal
fleet**: what servers exist, where they are, what manages them, how they
are classified, and how healthy they are.

The platform does not configure, provision or control hardware. It reads
from vendor management systems and BMCs, normalises what they report
into one vendor-neutral shape, and makes that queryable, classifiable and
alertable.

### Top three quality goals

| Priority | Goal | Why it dominates |
|---|---|---|
| 1 | **Correctness of reported state** | An inventory that is confidently wrong is worse than one that admits it does not know. This drives the `None`-means-unread contract, `parse_site_code` returning `None` rather than guessing, and recording classification conflicts instead of resolving them by luck. |
| 2 | **Scale to ~10,000 servers, headroom to 50,000+** | A real requirement, verified against genuine 50k datasets (ADR-0007), not a stretch goal. It decides pagination, projections, caching and collector design. |
| 3 | **Operability in an air-gapped site** | No internet at runtime *or* build time. Decides dependency choices, image bases, and how configuration reaches a deployment. |

### Stakeholders

| Role | Cares about |
|---|---|
| Infrastructure/platform operator | Is the inventory complete and current; which servers are unhealthy; can I find a machine by serial or MAC |
| Datacenter engineer | Per-server hardware detail, BMC address, fabric connectivity |
| Platform team (rule/policy author) | Classification rules and health policies behave predictably, and a change can be previewed before it lands |
| This repo's maintainers | Adding a vendor is "write a provider", not "change the core" |

---

## 2. Architecture Constraints

These are given, not chosen. Several architectural decisions only make
sense against them.

| Constraint | Consequence |
|---|---|
| **Air-gapped deployment.** No internet at runtime, and dependencies come from a local mirror. | `requirements.txt`/`pylock.toml` are generated exports (`docs/air-gap.md`); dependency versions are pinned to *what the mirror carries*, not to the newest release (`ucsmsdk==0.9.18`, `ucscsdk==0.9.0.8`). A 57.6 MB SDK is a real cost, which is why the Intersight collector does not use one (ADR-0017). |
| **~10,000 servers, headroom to 50,000+.** | Keyset pagination only, lean list projections, cache-aside Redis, and collector designs judged on requests-per-fleet rather than per-server. |
| **OpenShift/Kubernetes as the runtime.** | Helm chart; UBI9 base images; `Route` rather than `Ingress`; collectors are `CronJob`s. |
| **Python ≥3.12** (ADR-0015). | The compatibility floor for the target platform's available interpreter. |
| **Everything configurable must be configurable without a rebuild.** | Reinforced by ADR-0018: pushing a new image through an air-gapped mirror to rename a site is not acceptable, so the site list is configuration. |
| **The user is the sole visible contributor**, Conventional Commits. | CI derives published image versions from commit messages (ADR-0010). |

---

## 3. Context and Scope

### Business context

```
   Cisco UCS Central ─┐                                    ┌─ Operator (browser)
   Cisco Intersight ──┼──▶  Server Inventory Platform  ────┤
   Standalone BMCs ───┘         (this system)              └─ Prometheus (scrape)
   (Redfish)
```

| External | Direction | Exchanged |
|---|---|---|
| Cisco UCS Central + each domain's UCS Manager | in | Registered domains, service profiles, per-server inventory (XML API, `ucscsdk`/`ucsmsdk`) |
| Cisco Intersight (on-prem appliance) | in | Fleet inventory over an OData REST API, HTTP-Signature signed |
| Standalone BMCs | in | Per-server inventory over DMTF Redfish |
| Operator | in/out | React admin UI over the REST API |
| Prometheus | out | `/metrics` |

**Explicitly out of scope:** provisioning, firmware management, power
control, and any *write* to a vendor manager. Every collector is
read-only, and that is a safety property rather than a missing feature.

### Technical context

- **Inbound:** HTTPS to the FastAPI backend (`/api/v1/...`), plus
  unversioned `/health/live`, `/health/ready`, `/metrics` — unversioned
  on purpose, since the orchestrator consumes them and they must not move
  if `/api/v1` becomes `/api/v2`.
- **Outbound:** MongoDB (source of truth), Redis (cache), and — from
  collector pods only, never from the API — vendor APIs.
- **The API never talks to a vendor, and a collector never talks to the
  API.** MongoDB is the only thing connecting them. This is the single
  most load-bearing structural fact about the system.

---

## 4. Solution Strategy

| Goal | Approach |
|---|---|
| Add a vendor without touching the core | A `ServerInventoryProvider` port producing a vendor-neutral `ProviderServer`. A new vendor is a new `infrastructure/providers/<vendor>` module plus a `CronJob`. Nothing in the API, either engine, or the frontend changes. |
| Keep collection and serving independent | Separate processes with MongoDB between them. A collector failure degrades freshness, never availability. |
| Make classification and health *declarative* | Two engines over persisted rules/policies, editable in the UI, with deterministic resolution and preview-before-save. |
| Never let user input become an attack | No user regex ever reaches MongoDB; regex runs in Python under a timeout; search is anchored, escaped prefix matching; no `eval`/`exec` anywhere in the policy grammar. |
| Prove scale rather than assume it | Seed and load-test against genuine 10k/50k datasets (ADR-0007). |
| Exercise the real pipeline before real hardware exists | The fake generator implements the *same* provider port, so ingestion is exercised end to end by dev data exactly as by a collector. |

---

## 5. Building Block View

### Level 1 — the deployable units

| Block | Responsibility |
|---|---|
| **Backend API** (`backend/app`) | Serves the REST API; owns classification, health evaluation, search, pagination, caching. Never contacts a vendor. |
| **Collectors** (`tools/run_collector.py` + a provider) | One process per manager type, on a schedule. Reads a vendor, normalises, ingests. Never serves traffic. |
| **Frontend** (`frontend/`) | React admin UI. Talks only to the backend API. |
| **MongoDB** | Source of truth. |
| **Redis** | Cache only, cache-aside, never authoritative. |

### Level 2 — backend layering

```
backend/app/
  domain/           pure logic: models, ports, engines, value objects — no I/O, no framework
  application/      use-case orchestration (IngestService, ClassificationService, …)
  infrastructure/   MongoDB, Redis, logging, credentials, vendor providers
  api/              FastAPI routers — thin, no business logic
  middleware/       request id + timing
  observability/    Prometheus metrics
```

**Dependency direction is strictly inward.** `domain` imports nothing
from the other layers; `infrastructure` implements `Protocol`s declared
in `domain`. ADR-0018 kept this honest under pressure: the site catalog
is threaded in as a parameter rather than letting the domain read
`Settings`.

Detail for each subsystem is in `docs/architecture.md`: search/pagination/
caching, the classification engine, the health policy engine, and how
ingestion wires both together.

### Level 3 — the collector seam (the part built most often)

```
ServerInventoryProvider (Protocol)
    ├── provider_type: str
    ├── health_check() -> None
    └── list_servers() -> AsyncIterator[ProviderServer]
```

| Provider | Status | Shape |
|---|---|---|
| `ucs_central` (+ `ucs_manager` as its engine) | Implemented, validated against a live UCS Central and a UCS Platform Emulator | Central lists domains; each domain's own UCS Manager supplies inventory |
| `intersight` | Implemented; **field mapping never run against real data** (ADR-0017) | Fleet-wide OData list queries joined in memory |
| `redfish` | Implemented, validated | One BMC at a time from an inventory file |
| `fake` | Implemented | Deterministic dev/CI data through the same port |
| `openmanage`, `oneview` | **Not implemented** — `_PROVIDER_FACTORIES` raises a clear `NotImplementedError` rather than silently collecting nothing | — |

Two rules of this seam matter more than the rest:

1. **`None` means "could not read", and is not `0` or `()`.**
   `IngestService` carries the stored value forward for `None` and
   overwrites for a real value. Collapsing the two once wrote zeros over
   good data and reported a failed drive as recovered.
2. **A provider never declares a server's site.** It is parsed from the
   server's own name, so a misconfigured manager cannot mislabel
   everything it collects.

### Frontend

React 19 + react-router 8, TanStack Query for server state, TanStack
Table, Tailwind 4, Vite. Pages: sites overview (landing), inventory,
server detail (overview/hardware/network/connectivity tabs),
classification rules, health policies, and an audit history panel.

It holds **no** copy of the site list — it reads that from
`GET /api/v1/sites`, which is what let ADR-0018 change the site model
with no frontend change at all. It does still hold hardcoded copies of
`ManagerType`, guarded by `tests/unit/test_frontend_manager_types.py`
after those copies silently drifted.

---

## 6. Runtime View

### 6.1 A collection run

```
CronJob fires
  → resolve endpoint + credential from env      (ManagerNotConfiguredError names the missing vars)
  → provider.health_check()
  → for each server the vendor reports:
        normalize → ProviderServer
        name filter (INVENTORY_COLLECTOR_NAME_PATTERN)
        parse site from name (fallback: UCS org DN)
        classify → health-evaluate → audit → upsert     ← ONE write per server
  → write the Manager projection
  → exit 0 (complete) | 2 (not configured) | 3 (PARTIAL) | 1 (failed)
```

**Exit code 3 is the interesting one.** A run that wrote some servers but
could not see the whole fleet is neither success nor failure, and
reporting it as success is how a bad credential on one domain stays
invisible for weeks. For a fleet of independent BMCs it is the *normal*
outcome — which is why the guidance is to alert on staleness, never on
Job status.

### 6.2 A list request

```
GET /api/v1/servers?…
  → RequestContextMiddleware binds a request id
  → validate filters; decode + verify the HMAC-signed cursor
  → Redis lookup (cache-aside; any Redis error is a miss, never an error)
      miss → in-process coalescing (ADR-0007) → MongoDB keyset query
  → project to ServerSummary (no hardware subdocument)
  → cache, return
```

### 6.3 Editing a rule or policy

The engines are declarative and the edit path is preview-first: a draft
is spliced into the live rule/policy set and re-resolved against real
candidate servers, so the operator sees *what would actually happen*
rather than whether a pattern matches in isolation. Health preview in
particular re-runs full family resolution, because a low-precedence draft
can be legitimately shadowed.

---

## 7. Deployment View

```
OpenShift namespace
├── Deployment  backend API (N replicas)  ── Service ── Route
│      envFrom: <release>-api-config (ConfigMap)   ← INVENTORY_SITES lives here
│      env:     Mongo/Redis URIs from a Secret
├── CronJob  collector-ucs-central          (hourly, opt-in)
├── CronJob  collector-intersight           (hourly, opt-in)
├── CronJob  collector-redfish-standalone   (6-hourly, opt-in, ships suspended)
│      all three: envFrom the SAME api-config ConfigMap + the collector Secret
├── Secret   <release>-collector-credentials  (rendered from values, or bring your own)
├── MongoDB  ─┐  platform-provided, not deployed by this chart
└── Redis    ─┘
```

- **Images:** backend on `ubi9/ubi-minimal:9.8`; frontend built on
  `ubi9/nodejs-22` and served by `ubi9/nginx-124`. Both published to GHCR
  by CI, versioned from Conventional Commits (ADR-0010).
- **The ConfigMap is deliberately shared** between the API and every
  collector. They must agree on `INVENTORY_SITES`, because a collector
  derives each server's site at ingest — a collector with a stale list
  would write servers the API cannot name.
- **Credentials are one endpoint and one login per manager type, from the
  environment** (ADR-0012). No `Manager` document to create. The Redfish
  collector is the documented exception: its fleet comes from a mounted
  TOML inventory (ADR-0016).
- **Gap, stated plainly: there are no Kubernetes manifests for the
  frontend.** Only the backend has a Deployment/Service/Route, despite the
  frontend having had a working Containerfile since slice 1.
- **Gap: nothing deploys these images.** CI publishes; no GitOps/ArgoCD
  wiring exists.

---

## 8. Cross-cutting Concepts

| Concept | Where it lives | The one-line version |
|---|---|---|
| **Domain model** | `domain/models` | `Server` composed of `identity`, `profile_template`, `hardware`, `network`, `connectivity`, `classification`, `health`, `maintenance`, `openshift`; plus `Site`, `Manager`, `ClassificationRule`, `HealthPolicy`, `AuditEvent`. `Vendor` is `dell`/`cisco`/`hp`/`standalone`; `InstallationType` is `HOSTED_CLUSTER`/`UPI`/`UNCLASSIFIED` |
| **Error handling** | `exception_handlers` | RFC 9457 Problem Details, extended with a stable `code`, `request_id` and structured `details` (ADR-0002) |
| **Persistence rules** | `infrastructure/mongodb` | Datetimes stored as ISO 8601 **strings**; range/cursor queries must compare against that type (ADR-0006 — this caused a real silent-wrong-results bug) |
| **Search** | `domain/services/search_tokens` | Anchored, escaped prefix match over a multikey-indexed token array; structurally incapable of ReDoS or an unanchored scan (ADR-0004) |
| **Pagination** | `domain/services/cursor` | Keyset only, HMAC-signed cursor bound to the filter/sort combination |
| **Caching** | `infrastructure/redis` | Cache-aside; revision-keyed detail entries; every method returns a miss on error rather than raising |
| **Classification** | `domain/services/classification` | Total order `(priority, specificity, order, id)` computed in Python; conflicts recorded, not hidden (ADR-0005 for the sibling idea) |
| **Health policy** | `domain/services/health` | `policy_key` families — one winner per family, families independent. The platform's headline design decision (ADR-0005) |
| **Safety** | throughout | No user regex to Mongo; regex under timeout; closed condition grammar; templates rendered by explicit substitution, never `str.format` |
| **Sites** | `domain/value_objects/site` | A `SiteCatalog` from `INVENTORY_SITES`; a server's site is parsed from its own name (ADR-0011, ADR-0018) |
| **Observability** | `observability/`, `infrastructure/logging` | structlog with framework logs through the same pipeline; `http_requests_total`, `http_request_duration_seconds`, `cache_operations_total`, `mongo_ping_failures_total` |
| **Audit** | `application/services/audit_service` | Every state change recorded with an actor |
| **Secrets** | — | Never logged. No debug flag anywhere prints a credential, a signature or an `Authorization` header. `tests/unit/test_no_committed_secrets.py` fails the build on a committed PEM or password |

### Authentication — the honest current state

There is **no authentication and no authorization**. Not "permissive" —
absent. `app.dependencies.get_current_actor` returns a fixed
`unauthenticated` `Actor` so that audit events have *some* actor to
record; that is the entire scaffolding. Every endpoint is open to anyone
who can reach the Route.

This is a deliberate, repeatedly-confirmed deferral to the last slice,
and it is the release gate. Note that `CLAUDE.md` describes this as
"`AuthProvider`/RBAC scaffolding"; no such class exists, and this
document is the accurate one.

---

## 9. Architecture Decisions

The ADRs are the record; this is the index. Read the ADR, not a summary
of it.

| ADR | Decision |
|---|---|
| 0001 | MongoDB is the source of truth |
| 0002 | RFC 9457 error envelope |
| 0003 | Async PyMongo, not Motor (which entered deprecation May 2026) |
| 0004 | Token-prefix search instead of user regex |
| 0005 | `policy_key` shadowing — the headline design decision |
| 0006 | Audit cursors compare against stored **string** dates |
| 0007 | Query plans verified against real 10k/50k data; list reads request-coalesced |
| 0008 | Playwright E2E and the maintenance UI |
| 0009 | UCS Manager collector — validated against UCSPE, which found five real defects |
| 0010 | Image publishing and Conventional-Commit versioning |
| 0011 | Closed sites/vendors; site derived from the server's name |
| 0012 | Manager connections from environment; one manifest set |
| 0013 | SHA-pinned CI actions; Dependabot deliberately removed |
| 0014 | UCS Central multi-domain collector |
| 0015 | Python 3.12 compatibility floor |
| 0016 | Standalone Redfish collector |
| 0017 | Intersight collector |
| 0018 | Sites from configuration — supersedes part of 0011 |

---

## 10. Quality Requirements

### Quality tree

```
Correctness ── never report a value that was not read
            ── deterministic classification and health resolution
            ── a partial collection run is distinguishable from a complete one
Scalability ── 10k servers, headroom to 50k
Operability ── air-gapped install; actionable failure messages
Security    ── no user input reaches a regex/query engine unescaped; no secret ever logged
Modifiability ─ a new vendor is a new module; a site rename is a config change
```

### Scenarios

| # | Scenario | Response |
|---|---|---|
| Q1 | A collector's sub-resource query fails mid-run | That field reports `None`; the stored value is preserved; the run exits 3 (PARTIAL) naming what it could not read. **Never** zeros over good data. |
| Q2 | Redis becomes unreachable | Every read path falls back to MongoDB. Latency degrades; correctness and availability do not. `/health/ready` reports `degraded` without failing. |
| Q3 | MongoDB becomes unreachable | `/health/ready` fails (503) and the orchestrator stops routing traffic. |
| Q4 | An operator saves a pathological regex | Rejected at *write* time against a canary suite of pathological inputs. One that still times out at evaluation is skipped and counted, never stalling the run. |
| Q5 | 50,000 servers, concurrent list requests | Keyset pagination + lean projections + request coalescing. Measured p50/p95/p99 in ADR-0007. |
| Q6 | A site is renamed | One environment variable, pod restart. Reaches API, UI, filters, editors and seeded rules. No code change, no image rebuild. |
| Q7 | Two classification rules tie and disagree | Winner is deterministic (lowest id); the disagreement is persisted in `conflicts[]` so the authoring mistake surfaces. |
| Q8 | A collector is pointed at a wrong/half-configured vendor | Fails before any connection with a message naming the exact variables to set (exit 2). |
| Q9 | A new vendor collector is added | New provider module + factory entry + CronJob. No change to API, engines or frontend. |

---

## 11. Risks and Technical Debt

Ordered by what would hurt most. This section is the one most likely to
go stale — treat its date as load-bearing.

### High

| Risk | Detail |
|---|---|
| **No authentication at all** | Every endpoint is open to anyone who can reach the Route, including all write endpoints. Deliberate and confirmed, but it is the release gate and nothing should go to production without it. |
| **The Intersight collector's field mapping is mostly still unverified against real data** | Built entirely from the published contract; the DevNet sandbox is offline until ~2027. `tools/verify_intersight.py` against the user's own on-prem tenant (2026-09-01, 19 servers) confirmed auth, name resolution and — the highest-risk item — that `TotalMemory` is MiB as assumed (`docs/adr/0017`'s "first real tenant run"). A full `--dry-run` ingest has not been run yet, and everything else under ADR-0017's UNVERIFIED list (CPU/storage/adapter fields, region handling, clock-skew behaviour) is still contract-only. |
| **No staleness detection** | A CronJob pod is never scraped, so no collector-side metric can report its own absence. Nothing today answers "40 hosts have been failing for two weeks". `last_seen_at` is written on every ingest and read by nothing. This is the top item on the not-done list. |

### Medium

| Risk | Detail |
|---|---|
| The Redfish collector does not reach 10k | ~25 round trips per BMC; supported range ~400–1000 hosts per CronJob, sharded beyond that. Stated in ADR-0016 rather than hidden. |
| Intersight requires an on-prem appliance | A licensed Cisco product this platform does not control — a deployment dependency no other collector carries. |
| `INVENTORY_CURSOR_SECRET` has an insecure default | Documented in a code comment; **not enforced at startup**. A production deployment that forgets it gets forgeable cursors. |
| No rate limiting anywhere | |
| Mongo HA/backup and Redis persistence | Documented as "the platform's problem"; nobody has actually stood either up. |
| Manual dependency maintenance | Dependabot was deliberately removed (ADR-0013), making pin currency and CVE checks a standing quarterly chore. |

### Low / accepted

- No Kubernetes manifests for the frontend; no GitOps/CD wiring.
- No alerting rules or dashboards over the existing metrics.
- A *syntactically valid* typo in `INVENTORY_SITES` (`tvl` for `tlv`)
  cannot be caught at startup — only by looking at the resulting
  inventory. `--dry-run` prints the resolved site per server for this.
- `INTERSIGHT` and `ONEVIEW` have collectors that have **never run**
  against live vendor hardware (ADR-0017, ADR-0022); `tools/verify_intersight.py`
  and `tools/verify_oneview.py` are the outstanding actions on both.
- Frontend copies of `ManagerType` are hand-maintained (now guarded by a
  test after they silently drifted).
- The repo is mid-migration to the docstring convention (CLAUDE.md
  convention 8); older files still carry the previous inline-comment
  style.

---

## 12. Glossary

| Term | Meaning |
|---|---|
| **Collector** | A scheduled process that reads one vendor manager type and ingests what it finds. One CronJob per manager type. |
| **Provider** | The code implementing `ServerInventoryProvider` for a vendor. Lives in `infrastructure/providers/<vendor>`. |
| **`ProviderServer`** | The vendor-neutral DTO a provider emits — flatter than the domain `Server`, already normalised, not yet correlated. |
| **Manager type** | How a server is reached (`UCS_CENTRAL`, `INTERSIGHT`, `REDFISH_STANDALONE`, …). Distinct from **vendor**, which is who built it. |
| **`source_provider`** | Which collector found a given server. Filterable in the UI. |
| **Site** | A location, identified by a short code embedded in server hostnames (`ocp4-prod-**tlv**-infra-01`). Configured via `INVENTORY_SITES`. |
| **Classification** | Deciding what a server *is* (`HOSTED_CLUSTER`, `UPI`, `UNCLASSIFIED`) from declarative rules. |
| **`policy_key` family** | A set of health policies competing for one winner, so a scoped policy can *replace* a global default rather than firing alongside it. |
| **Specificity** | Scope precision as powers of two (`site:4 + manager:2 + vendor:1`), so a more specific scope strictly outranks a less specific one. |
| **Service profile** | UCS's logical server definition. **The source of a UCS server's real name** — `computeBlade.name` is empty in practice. |
| **IMM** | Intersight Managed Mode. Servers Intersight manages directly, as opposed to `UCSM`-mode servers that UCS Central owns. |
| **PARTIAL run** | Exit code 3: some servers were written, but the run did not see the whole fleet. |
| **UCSPE** | Cisco's free UCS Platform Emulator — the test target that validated the UCS collector. |
| **PVA** | Intersight Private Virtual Appliance: on-prem Intersight, the only form reachable from an air-gapped site. |
