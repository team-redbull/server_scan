# Audit: domain, API and application layers (2026-09-06)

Read-only audit for the production-hardening pass. Scope: `backend/app/domain/`,
`backend/app/api/`, `backend/app/application/`, `backend/app/dependencies.py`,
`backend/app/main.py`, `backend/app/errors.py`, `backend/app/exception_handlers.py`,
`backend/app/config/`. No source code was changed.

Every unusual-looking thing was checked against `docs/adr/*` first. Anything an ADR
explains is not listed as a finding. Anything that looks wrong with no ADR behind it
is in "Questions for the user", not asserted as a bug.

---

## 1. Docstring conformance to convention 8

Measured with `ast`: every `class`, `def` and `async def` in scope, checking for a
docstring and for the `Args:` / `Returns:` / `Yields:` sections that item actually
requires (arguments other than `self`/`cls`; a real return annotation or a `return
<value>`; a `yield`).

| | Count | Share |
|---|---:|---:|
| **Google-style, complete** (all required sections present) | **80** | **22%** |
| **Docstring in the older style** (prose only, or missing a required section) | **39** | **11%** |
| **No docstring at all** | **244** | **67%** |
| **Total items in scope** | **363** | |

Across 62 files in scope, **4 files conform fully** — every item in them has a
complete Google-style docstring:

- `backend/app/domain/value_objects/site.py` (12 items)
- `backend/app/domain/value_objects/gpu_catalog.py` (12 items)
- `backend/app/domain/value_objects/nic_names.py` (5 items)
- `backend/app/domain/models/openshift.py` (1 item)

A further 12 files trivially conform because they contain no functions or classes at
all (the `__init__.py` re-export shims, and `domain/value_objects/gpu_models.py`,
which is pure data).

**The other 46 files are non-conforming.** The largest concentrations:

| File | ok | partial | none |
|---|---:|---:|---:|
| `backend/app/errors.py` | 2 | 2 | 28 |
| `backend/app/api/v1/servers.py` | 1 | 4 | 14 |
| `backend/app/application/services/ingest.py` | 6 | 2 | 13 |
| `backend/app/domain/services/health/metrics.py` | 1 | 0 | 9 |
| `backend/app/domain/models/hardware.py` | 0 | 0 | 9 |
| `backend/app/domain/services/health/conditions.py` | 1 | 2 | 8 |
| `backend/app/domain/models/health_policy.py` | 0 | 0 | 8 |
| `backend/app/api/v1/classification_rules.py` | 0 | 0 | 8 |
| `backend/app/api/v1/classification_schemas.py` | 2 | 0 | 8 |
| `backend/app/api/v1/health_policies.py` | 0 | 1 | 8 |
| `backend/app/domain/services/health/evaluate.py` | 0 | 1 | 7 |
| `backend/app/exception_handlers.py` | 0 | 0 | 7 |
| `backend/app/domain/services/classification.py` | 1 | 0 | 6 |
| `backend/app/domain/services/regex_engine.py` | 0 | 0 | 6 |
| `backend/app/api/v1/health_policy_schemas.py` | 4 | 0 | 6 |

Full per-file table and the per-item list is reproducible with the census script
approach described at the end of this document.

**Two systematic patterns account for most of the 244:**

1. **Pydantic models and dataclasses have no class docstring at all** — `Cpu`,
   `Memory`, `Storage`, `Gpu`, `Psu`, `Power`, `Hardware`, `Identity`, `Server`,
   `PolicyScope`, `EvidenceField`, `PolicyStats`, `HealthPolicy`, `RuleScope`,
   `RuleFlags`, `RuleStats`, `ClassificationRule`, `Actor`, `AuditEvent`,
   `Classification`, `Health`, `Maintenance`, `Manager`, `Site`, `BmcInfo`,
   `NetworkInterface`, `NetworkInfo`, `Connectivity`, `ConnectivityAttachment`.
   Convention 8 says "every function, method and class". These carry field-level
   `#` comments instead, which is the pre-2026-08-18 style the convention reverses.
2. **`errors.py`'s 24 `AppError` subclasses** each carry only `status_code` and
   `code`. This is the single densest block of missing docstrings in the scope and
   the cheapest to fix — the module docstring already explains the mechanism, so
   each subclass needs one summary line saying when it is raised.

---

## 2. Findings

Ranked by severity, then by effort within a severity. **file:line** is the anchor.

### Correctness

| # | file:line | Finding | Fix | Effort |
|---|---|---|---|---|
| C1 | `backend/app/application/services/bootstrap.py:108` | `ensure_default_health_policies` seeds a default policy only when its `name` is absent, and **never re-syncs a drifted definition** — unlike `ensure_default_classification_rules` directly above it, which does. A shipped default whose condition, severity or `message_template` changes in code never reaches a database seeded before the change. This is not hypothetical: `facts.py:49` changed `power.failed_psu_count` from matching `"OK"` to matching `"DOWN"`, and `facts.py:31` changed the failed-drive comparison from `"FAILED"` to `CRITICAL`. Any default policy edited the same way is silently stale on every existing deployment. CLAUDE.md's stated intent — "rules and policies ship with the platform, so every deployment classifies and scores identically" — is only true for rules today. | Apply the same `_definition_of` / `_resynced` pair to policies. `_ADMIN_OWNED_RULE_FIELDS` generalises: for a policy it is `("id", "enabled", "stats", "created_at", "created_by")` — identical field names. Roughly 20 lines, mostly shared with the rule path. | S |
| C2 | `backend/app/application/services/classification_service.py:252` | `except RegexTimeout: continue` inside `preview()`'s scan loop swallows the timeout with **no logging and no counter**. A pattern that times out against every candidate reports `matched_count=0`, indistinguishable from a pattern that legitimately matches nothing. The domain `classify()` gets this right — `classification.py:135` records a `RuleTimeoutError` per rule — so the correct behaviour already exists two files over and is not reused here. (Currently unreachable; see D1.) | Count timeouts and return them on `PreviewResult`, or at minimum `logger.warning`. If D1 is taken and `preview` is deleted, this goes with it. | S |
| C3 | `backend/app/domain/services/regex_engine.py:98` | `RegexModuleEngine.search` calls `self._compile(...)` outside any `try`. `regex.error` from an uncompilable stored pattern escapes as a bare `regex.error` — `classify()` only catches `RegexTimeout`, so it propagates to `IngestService.ingest`'s `except Exception` and fails **that whole server**, and it will do so for every server on every run, since the pattern is per-rule not per-server. `validate()` two methods up handles exactly this case and converts it to `RegexUnsafeError`. Low likelihood today (patterns are seeded, and write endpoints are gone) but the blast radius is the entire fleet. | Wrap the `_compile` call the same way `validate` does and raise `RegexUnsafeError`; `classify()` then needs to catch it alongside `RegexTimeout` and record it in `ClassificationResult.errors`. | S |
| C4 | `backend/app/domain/services/health/evaluate.py:132` | `evaluate_condition(...)` is called with no exception boundary. A stored condition whose operator/value pair is type-incompatible with the resolved fact (`_eval_scalar`'s `actual > value` with a `None` or a `str` on one side) raises `TypeError`, which escapes `evaluate_health` and, via `IngestService._ingest_one`, fails that server's whole ingest. `validate_condition` is the guard, and its docstring says so explicitly — "Called when a policy is created/updated, never at evaluation time" — but with the write endpoints removed there is now **no path that calls it on stored data at all**. The invariant it protects is unenforced end to end. | Either call `validate_condition` over the loaded policy set once per `evaluate_health` run (cheap: policies number in the dozens), or catch per-policy in the loop and record a `PolicyStats.error_count` / quarantine, mirroring how `classify()` handles a per-rule failure. The second matches the existing `quarantined` field, which nothing currently writes. | M |
| C5 | `backend/app/api/v1/servers.py:302-318` | `get_server`'s revision-pointer cache has a **write-order race**. The pointer (`_revision_pointer_key`) is written *before* the detail payload. Between those two `await`s, a concurrent reader can read the new pointer, miss on the not-yet-written detail key, and fall through to Mongo — harmless. But `_invalidate_detail_cache` deletes only the pointer, so a `reclassify`/`recalculate`/maintenance write that interleaves between these two `set` calls leaves a **stale pointer written back after the delete**, pointing at revision N while the document is at N+1, for the full `SERVER_DETAIL_TTL_SECONDS`. The detail key it names still holds revision N's payload, so the endpoint serves stale data until TTL. | Write the detail payload first, then the pointer. That makes the pointer's presence imply the payload's, and shrinks the stale window to the interval between the read of `server.revision` and the pointer write. | S |
| C6 | `backend/app/api/v1/servers.py:367`, `:411` | `reclassify_server` and `recalculate_server_health` do read-modify-write on `server.revision` with **no optimistic-concurrency check**: `repo.get_by_id` then `server.revision += 1` then `repo.upsert(server)`. Two concurrent requests (or one request racing a collector run) both read revision N, both write N+1, and the second silently discards the first's classification result. `RevisionConflictError` and `ErrorCode.REVISION_CONFLICT` exist in `errors.py:127` and are **raised nowhere in the codebase** — the machinery for this was built and never wired up. Same pattern in `maintenance_service.py:52` and `:72`. | Make `ServerRepository.upsert` conditional on the read revision (`{"_id": id, "revision": n}`) and raise `RevisionConflictError` on a zero-match update. Touches the port, the Mongo implementation, and four call sites. | M |
| C7 | `backend/app/api/v1/servers.py:100-109` | `_extract_raw_filters` reads `request.query_params.items()`, which for a repeated parameter (`?vendor=dell&vendor=cisco`) silently keeps **one** value with no error. Every other malformed-input case in this router raises (`UNKNOWN_FILTER`, `_parse_bool`'s `ValidationAppError`). A caller building a multi-select filter gets a silently narrowed result set rather than a 400. | Use `request.query_params.multi_items()` and raise `ValidationAppError` when a key appears more than once, or support the multi-value case explicitly as `$in`. | S |

### Maintainability

| # | file:line | Finding | Fix | Effort |
|---|---|---|---|---|
| D1 | `backend/app/api/v1/classification_rules.py:50,57,65,69,73`; `backend/app/api/v1/health_policies.py:63,70,78,82` | **Dead code left by the read-only migration** (commits `27b20a8`, `f9ab059`). Nine module-level functions are defined and referenced by nothing: `_regex_engine`, `_classification_service`, `_scope_from_schema`, `_flags_from_schema`, `_audit_service` (rules router); `_server_repo`, `_health_policy_service`, `_audit_service`, `_validate_and_build` (policies router). Verified by grep across `backend/`, `tests/` and `tools/`: each appears only at its own definition, or is reachable only from another dead one. Ruff's `F401` catches the now-unused *imports* these keep alive, which is why nothing failed — the imports are still "used", by dead code. | Delete all nine plus the imports they alone justify (`ClassificationService`, `RegexEngine`, `RegexModuleEngine`, `Settings`/`get_settings`, `RuleFlags`, `RuleScope`, `RuleFlagsSchema`, `RuleScopeSchema`, `AuditService`, `MongoAuditEventRepository`, `MongoServerRepository`, `HealthPolicy`, `PydanticValidationError`). Purely subtractive. | S |
| D2 | `backend/app/application/services/classification_service.py:198`, `backend/app/application/services/health_policy_service.py:222` | **Both `preview()` implementations are dead**, along with everything they alone reach: `PreviewResult` (×2), `PreviewMatch`, `_build_draft_policy`, `_extract_field` (the classification_service copy), and `_validate_pattern`'s second caller. The write-path validators `validate_rule_write`, `validate_policy_write`, `validate_system_field_lock` and `_validate_scope_source_coherence` are likewise reachable **only from `tests/unit/application/services/`** — grep confirms no production caller. That is roughly 250 lines across the two services, plus two test files that now assert the behaviour of unreachable code. | This is a decision, not a mechanical fix — see Q1. If the answer is "gone for good", deleting these two services down to `classify_server` and `evaluate_server` removes ~250 lines of application code and two whole test modules, and drops `ClassificationService`'s `MongoClientHolder` dependency (it exists only to give `preview` a raw collection). | M |
| D3 | `backend/app/domain/services/classification.py:23` vs `backend/app/application/services/classification_service.py:265` | **Two `_extract_field` functions, same name, same job, different signatures** — one over `ClassifiableServer`'s six loose arguments, one over a `Server`. They must agree on which of `CLASSIFIABLE_FIELDS` maps to what, and nothing enforces that: the domain one handles `hostname`, the application one deliberately returns `None` for it. A future field added to `CLASSIFIABLE_FIELDS` has to be added in both, and forgetting the second silently previews zero matches. | If D2 removes `preview`, this resolves itself. Otherwise: have the application one build a `ClassifiableServer` from the `Server` and call the domain function — the mapping then exists once. | S |
| D4 | `backend/app/api/v1/classification_rules.py:1`; `backend/app/api/v1/health_policies.py:1`; `backend/app/application/services/classification_service.py:33` | **Module docstrings describe endpoints that no longer exist.** Both routers open with "CRUD + preview" and are GET-only. `classification_rules.py:8-10` says "cross-field validation lives in `validate_rule_write`" — this router calls it nowhere. `classification_service.py:33-37` explains `validate_rule_write` being a free function "because both the create and update code paths in `app.api.v1.classification_rules` need to run it" — neither path exists. A session reading these to orient itself is actively misled. | Rewrite the three docstrings to describe read-only routers. Keep the *reasoning* about anchored patterns and the `enabled_only` single-source-of-truth argument — those are still true and still load-bearing. | S |
| D5 | `backend/app/domain/models/health_policy.py:8`; `backend/app/domain/models/server.py:4`; `backend/app/application/services/bootstrap.py:12` | **Three docstrings name modules and types that do not exist.** `health_policy.py:8` points at `app.domain.services.health.resolution` (the module is `evaluate`). `server.py:4` points at `app.domain.services.identity, slice 2` (never built). `bootstrap.py:12` says a default rule's pattern "is generated from `SiteCode`" — `SiteCode` was deleted by ADR-0018 and replaced by `SiteCatalog`. | Repoint the first two, and update the third to `SiteCatalog` / ADR-0018. The *facts* in all three are still correct; only the names are stale. | S |
| D6 | `backend/app/domain/models/health_policy.py:28` | `PolicyMode = str` is defined, commented, and **used nowhere** — `HealthPolicy.mode` is annotated `str` directly. A type alias with one definition and zero uses. | Delete it, or actually use it on `mode` (in which case make it `Literal["EVALUATE", "SUPPRESS"]`, which would let ty catch what `_mode_is_known` currently catches at runtime). | S |
| D7 | `backend/app/domain/ports/repository.py:79` | `ServerRepository.count(filters)` is declared on the port and **called only from `tests/integration/test_server_repository.py:279`**. No production code path uses it — `with_count` on `list_page` covers the real need. A port method exists to constrain implementations; one nothing calls constrains nothing. | Delete from the port and the Mongo implementation, and drop the test — or, if it is kept as a deliberate future affordance, say so in a one-line docstring (it currently has none, per §1). | S |
| D8 | `backend/app/config/settings.py` | **67% of this file is comment or docstring** (258 comment lines + 16 docstring lines out of 412) — the highest ratio in the scope by a wide margin. It reads as documentation with settings embedded, not the reverse. Convention 8 explicitly reverses this style. **Several of these comments are hard-won facts that must be MOVED, not deleted**, with provenance intact: the `gpu_models` field-name bug (lines 96–125, confirmed live — `INVENTORY_GPU_MODELS` was silently a no-op under the old field name `gpu_model_catalog`); `oneview_page_size` (lines 208–212 — `count=-1` means 64, not "all", on `/rest/server-profiles`); `ucs_manager_*` having a login but no endpoint and *why* (lines 160–167); the `ome_bmc_*` two-login exception (lines 237–245); the `host = "0.0.0.0"` justification (lines 39–44). | Move each block to where it belongs and leave a one-line pointer: the GPU one to `docs/adr/0021`; the OneView ones to `docs/hpe-collectors.md` (which already holds the `count=-1` fact — confirm before moving, and keep whichever statement is more precise); the UCS one to `docs/adr/0014`; the Dell one to `docs/adr/0020`. The `0.0.0.0` note is a genuine one-line pin and should stay inline. Target roughly 60 comment lines. | L |
| D9 | `backend/app/domain/ports/provider.py` (52% prose), `backend/app/domain/models/server.py:42-73` (`ProfileTemplate`'s 31-line four-vendor comparison), `backend/app/application/services/ingest.py:1-32` | Three more modules whose opening prose exceeds 15 lines and argues a decision rather than describing an interface. `ProfileTemplate`'s docstring is a genuine four-vendor research artifact — the UCS `srcTemplName` / Intersight `SrcTemplate` / OneView `serverProfileTemplateUri` / OME "no persistent field" comparison. **That is exactly the kind of fact CLAUDE.md forbids dropping.** | Move `ProfileTemplate`'s vendor table to a new `docs/profile-templates.md` (or into ADR-0022, which already discusses OneView profiles) and leave the class with a two-line summary plus a pointer. `ingest.py`'s correlation-simplification paragraph belongs in an ADR — there is no ADR for the `(vendor, serial_normalized)` correlation key today, which is a real gap given how often CLAUDE.md cites it. `provider.py`'s belongs beside the collector architecture notes. | M |
| D10 | `backend/app/application/services/ingest.py:460-715` | `IngestService._build_server` is **256 lines doing five things**: identity/BMC normalisation, network-interface mapping, connectivity mapping, hardware carry-forward (13 `_carry_forward` calls), site derivation, then classification and health evaluation. It is the longest function in the scope by a factor of three. The `_carry_forward` design itself is excellent (see §3); it is the assembly around it that has accreted. | Extract `_identity_and_network(ps, existing)`, `_hardware(ps, existing_hardware, unread)` and `_connectivity(ps, now)` as module-level pure functions taking the `unread` accumulator. `_build_server` then reads as the six-step outline it actually is. No behaviour change; the `unread` list must stay a single accumulator threaded through, which is what makes this mechanical rather than risky. | M |

### Performance

| # | file:line | Finding | Fix | Effort |
|---|---|---|---|---|
| P1 | `backend/app/application/services/classification_service.py:195` and `backend/app/application/services/health_policy_service.py:212`, driven from `backend/app/application/services/ingest.py:704` and `:711` | **The one genuine N+1 in this scope, and it is on the 10,000-server path.** `IngestService._build_server` calls `classify_server` and `evaluate_server` once per server. `classify_server` does `rule_repo.list_all(enabled_only=True)` — a full `classification_rules` collection read plus a `ClassificationRule.model_validate` per document. `evaluate_server` does `policy_repo.list_all()` — the same for `health_policies`. Neither repository caches (verified: both are a bare `find(...).to_list(length=None)` plus a list comprehension of `model_validate`). A 10,000-server UCS Central run therefore issues **20,000 collection reads** and re-validates the same few dozen Pydantic documents 20,000 times, to get an answer that cannot change during the run. ADR-0007 covers request coalescing on the *API read* path; nothing covers this. | Load both sets once per ingest run and pass them down. The smallest shape that does not change any signature: give `ClassificationService` and `HealthPolicyService` an explicit `async def load(self)` that populates a per-instance list, and have `tools/run_collector.py` / `IngestService.ingest` call it before the `async for`. Do **not** use `@lru_cache` — these are async and the API process needs the reload-on-write behaviour. Expect the dominant cost of a collector run to move back to the vendor API where it belongs. | M |
| P2 | `backend/app/api/v1/servers.py:126`, `backend/app/api/v1/classification_rules.py:50` | `_regex_engine` is a **FastAPI dependency**, so a fresh `RegexModuleEngine` is constructed per request — and `RegexModuleEngine.__init__` builds its `lru_cache(maxsize=1024)` compilation cache in the constructor (`regex_engine.py:53`). The cache is therefore thrown away every request and never gets a hit on the API path. Every reclassify recompiles every pattern from scratch. (On the ingest path the engine is long-lived, so the cache works as designed there.) | Build one module-level engine per router the way `_METRIC_REGISTRY` already is at `servers.py:123` — the settings it reads (`regex_max_pattern_length`, `regex_match_timeout_seconds`) are process-wide and cannot vary per request. Two lines. | S |
| P3 | `backend/app/application/services/classification_service.py:244` | `preview()` runs `Server.model_validate(doc)` on up to `max_scan=5000` raw documents in order to read **one** of four scalar fields off each. Full Pydantic validation of a document with eight nested sub-models, discarded immediately. (Currently unreachable; see D2.) | If `preview` survives Q1: add a `projection={...}` to the `find` and read the four fields off the raw dict. If it does not, moot. | S |
| P4 | `backend/app/api/v1/events.py:39` | `AuditEventResponse.model_validate(e.model_dump())` — a full dict round-trip per event to convert between two models with the same field names, on every page of up to 200 events. | `AuditEventResponse.model_validate(e, from_attributes=True)`, or drop the second model if it is field-identical. | S |

### Consistency

| # | file:line | Finding | Fix | Effort |
|---|---|---|---|---|
| S1 | `backend/app/api/v1/servers.py:66,115,175`; `backend/app/api/v1/sites.py:39,76,190`; `backend/app/api/v1/health_policies.py:46,66`; `backend/app/application/services/health_policy_service.py:42,195` | **The `ServerRepository` port is declared and then bypassed.** Its own docstring (`ports/repository.py:3-5`) states: "Nothing in `application/` or `api/` talks to PyMongo directly — every server read/write goes through this interface." In practice every API dependency and `HealthPolicyService` annotate the **concrete** `MongoServerRepository`; only `IngestService` and `MaintenanceService` type on the port. Two consequences: `facet_breakdown` (`servers.py:289`) is called on a method that **exists on the concrete class but not on the port**, so the port no longer describes the real contract; and the swappability the port exists to provide is unavailable in exactly the layer the docstring names. | Add `facet_breakdown` to the `ServerRepository` Protocol (with `FacetRow` moved to `domain/ports/repository.py` beside `SiteBreakdownRow`, which it mirrors), then change the annotations in the three routers and `HealthPolicyService` to the port. The dependency providers still construct the concrete class — only the annotations change. | M |
| S2 | `backend/app/api/v1/schemas.py:44` | An API schema module imports `FacetRow` **from `app.infrastructure.mongodb.server_repository`**. `api/` reaching directly into `infrastructure/` for a type inverts the layering the rest of the codebase keeps. `SiteBreakdownRow`, the exact analogue, lives correctly in `domain/ports/repository.py` and is imported from there by `sites.py:36`. | Move `FacetRow` next to `SiteBreakdownRow`. Resolves alongside S1. | S |
| S3 | `backend/app/application/services/classification_service.py:64,186,237` | `ClassificationService` holds a `MongoClientHolder` and reaches a **raw `AsyncCollection`** (`self._mongo.db[SERVERS_COLLECTION]`), building its own `find(...)` — the only place in `application/` that talks to PyMongo. It exists solely to serve `preview()`. `HealthPolicyService` solves the identical problem through `server_repo.list_page(...)` (`health_policy_service.py:273`). Two answers to one question, in adjacent files. | If `preview` survives Q1, route it through `list_page` the way the health service does, and drop the `mongo` constructor argument entirely. If not, this goes away with D2. | S |
| S4 | `backend/app/api/health.py:37` | `/health/ready` sets a 503 status directly on the `Response` and returns a hand-shaped body. It is the **one endpoint that bypasses the RFC 9457 envelope** (ADR-0002) on an error status. Arguably correct — see Q3 — but it is currently undocumented as a deliberate exception, so the next reader cannot tell it from an oversight. | Whatever the answer to Q3, record it: either raise `ServiceUnavailableError` (which already exists, `errors.py:149`) or add one line to the module docstring saying orchestrator probes deliberately get a plain body. | S |
| S5 | `backend/app/api/v1/events.py:27-28` vs `backend/app/config/settings.py:71-72` | Events pagination hardcodes `_DEFAULT_PAGE_SIZE = 50` / `_MAX_PAGE_SIZE = 200` at module level; servers pagination reads `settings.default_page_size` / `settings.max_page_size`, whose values are **also 50 and 200**. Two sources for the same two numbers, already agreeing by coincidence rather than by construction. | Read the settings in `events.py` too, or state in a comment why events deliberately do not follow the configured limits. | S |
| S6 | `backend/app/api/v1/servers.py:182` vs `:186` | `page_size` is validated in two places with two mechanisms: FastAPI's `Query(ge=1)` produces a `RequestValidationError` → 422 `VALIDATION_ERROR`, while the upper bound is checked by hand and raises `PageSizeTooLargeError` → 422 `PAGE_SIZE_TOO_LARGE`. Both are 422 and both render through the envelope, so this is cosmetic — but a client sees two different `code`s for two halves of one constraint. The `le=` bound cannot be declared on the `Query` because the maximum is a setting, which is presumably why it is hand-rolled; that reason is not written down. | One comment saying the upper bound is settings-driven and so cannot be a `Query` constraint. | S |

### Type safety

| # | file:line | Finding | Fix | Effort |
|---|---|---|---|---|
| T1 | — | **No `# type: ignore[code]` anywhere in scope.** Checked specifically, since CLAUDE.md flags mypy-style coded suppressions as live bugs under ty. Zero occurrences in `domain/`, `api/`, `application/` or the four root modules. The one suppression in the codebase is `app.infrastructure.singleflight`'s, which is out of scope and already documented. Nothing to fix. | — | — |
| T2 | `backend/app/domain/services/health/conditions.py:52,56,152,168`; `backend/app/domain/services/health/metrics.py:38,76`; `backend/app/domain/services/health/facts.py:18` | `Any` flows through the entire health-evaluation path: `Condition.value: Any`, `Condition.equals: Any`, `facts: dict[str, Any]`, `MetricDef.resolver: Callable[[dict[str, Any]], Any]`. This is what makes C4 possible — no static check can see that a `GT` against a `LIST_STRING` metric is a `TypeError` waiting to happen; only `validate_condition` can, and it is no longer called on stored data. **This is a defensible design** (the condition grammar really is dynamic over heterogeneous facts), so the fix is not "add types" but "make the runtime guard actually run" — i.e. C4. Recorded here because it is the reason C4 has no cheaper fix. | See C4. | — |
| T3 | `backend/app/application/services/classification_service.py:200`, `backend/app/application/services/health_policy_service.py:224` | Both `preview()` methods take `draft_policy_input: dict[str, Any]` / `draft_rule_input: dict[str, Any]` — an **untyped dict crossing an application-service boundary**, with every field pulled out by `.get()` and hand-validated. Elsewhere in this codebase a request body is always a Pydantic schema. (Currently unreachable; see D2.) | If preview survives Q1, take a Pydantic draft schema. Otherwise moot. | S |

### Silent failure paths — assessed, mostly clean

Checked every `except` in scope. Results, for the record:

- **No bare `except:` anywhere in scope.**
- `ingest.py:316` `except Exception` around `_ingest_one` — **correct and deliberate**. It logs with `logger.exception` (traceback preserved), counts into `IngestSummary.errors`, and continues. It cannot swallow `CancelledError`, which derives from `BaseException` since Python 3.8 and this repo floors at 3.12 (ADR-0015). This is the right shape for a per-item loop in a batch job.
- `classification_service.py:167` `except Exception` in `_validate_pattern` — broad, but it re-raises as `RegexInvalidAppError` with `from exc`, chaining preserved. The inline comment states the intent. Acceptable.
- `template.py:66` `except (ValueError, TypeError)` falling back to `str(value)` — narrow and correct for a rendering path that must never fail.
- `cursor.py:128,139` — narrow tuples, both re-raise as the right `AppError`. The `from None` at line 129 deliberately drops the chain so a malformed cursor cannot leak internals; that is right for a client-facing error.
- **C2 is the only genuinely silent one** — no log, no counter, no re-raise.

### Input validation at trust boundaries — assessed, strong

- Filter keys: whitelisted, `UnknownFilterError` on anything else (`search.py:100`). Sort fields: whitelisted (`search.py:113`). Search string: length-bounded and `re.escape`d, single choke point (`search.py:121`).
- Cursors: HMAC-SHA256 verified with `hmac.compare_digest` **before** the payload is parsed (`cursor.py:131`), then the filter binding is compared with `compare_digest` too. Correct order; a forged cursor never reaches `json.loads`.
- Templates: tokenised with `Formatter().parse()` and rendered by explicit substitution, never `format_map`. The docstring at `template.py:71` is right that not calling `format` **is** the enforcement.
- Regex: length cap plus canary-timing probe at validate time, `timeout=` on every match.
- The gaps are C7 (repeated query params) and the fact that filter *values* are not validated against their enums — `?vendor=nonsense` returns an empty page rather than a 400. That is arguably fine and is listed as Q4 rather than a finding.

---

## 3. What is already good in this scope

Not a courtesy section — these are specific things worth not breaking during the
hardening pass.

- **`_carry_forward` (`ingest.py:157`) is the best-designed function in the scope.**
  Three-state `None`/stored/default resolution, and — the part that is easy to miss —
  it is *also* the single choke point where `unread_fields` is recorded, which is why
  the recording lives there rather than at 13 call sites. The docstring explains both
  the mechanism and the incident that motivated it. This is exactly what convention 8
  asks for.
- **`unread_fields` is recomputed, never merged** (`ingest.py:478`, `server.py:124`),
  and "never successfully read" is deliberately not expressible. Both the code and
  two independent docstrings say so. That is a subtle invariant stated three times in
  the places someone would break it.
- **The RFC 9457 envelope is genuinely centralised.** `register_exception_handlers`
  is the only place a client-facing error body is constructed; four handlers cover
  `AppError`, `RequestValidationError`, `StarletteHTTPException` and the catch-all.
  The per-class log-level split (INFO for expected, ERROR with `exc_info` for
  unhandled) is right, and the catch-all leaks neither message nor traceback. `/health/ready`
  (S4) is the sole exception and may well be a correct one.
- **`policy_key` family resolution (`evaluate.py:79`) is clean and correct.**
  Scope-match, sort by `policy_key`, `groupby`, sort each family by
  (specificity DESC, priority DESC, id ASC), head wins, the rest are recorded as
  `ShadowedEntry` rather than discarded. Disabled policies are deliberately included
  in the family so a disabled high-priority scoped policy switches a default off —
  that is the whole ADR-0005 mechanism and it is implemented exactly as described.
  The deterministic `for key in sorted(winners)` output order is a nice touch.
- **Keyset pagination is done properly.** No `skip`/`offset` anywhere. Signature
  verified before payload parse. The filter/sort/page_size binding hash means a
  client that changes a filter mid-pagination gets `CURSOR_FILTER_MISMATCH` rather
  than a silently wrong page — a failure mode most implementations ship.
- **Classification resolution never trusts database order** (`classification.py:3`).
  The sort key *is* the resolution order, computed in Python. The "keep scanning
  only while precedence is tied, purely to record conflicts; the winner never
  changes" loop (`classification.py:146-161`) is a genuinely subtle piece of logic
  that is both correct and explained.
- **The `ALL` operator refuses vacuous truth** (`conditions.py:191`): "all NICs are
  UP" over an empty list returns `False`, because reporting healthy from a
  collection gap is worse than reporting unknown. Same instinct as the carry-forward
  work, applied somewhere else entirely.
- **`ManagerConnection.__repr__` is redacted** (`credentials.py:54`) so a password
  cannot reach a traceback frame or a debugger. Small, easy to forget, done.
- **`ServerSummary` vs `ServerDetail`** — the list projection genuinely omits the
  hardware subdocument, and the module docstring gives two concrete reasons for the
  detail schema existing separately (the `_id` alias leak, and the "should this be
  public?" seam). Both hold up.
- **`_pivot` in `sites.py:144`** seeds every configured site before folding rows in,
  so a site with zero servers renders as an empty card rather than a missing one,
  and an unconfigured leftover `site_id` lands in `unassigned` rather than being
  dropped — the totals always add to the fleet size. That invariant is stated and
  actually held.
- **ADR discipline is real.** Every unusual thing checked during this audit had an
  ADR behind it — the string dates (0006), the sites-from-config threading (0018),
  the `policy_key` families (0005), the closed vendor enum (0011/0016), the GPU
  catalog fallback (0021). The domain layer **never reads `Settings`**, verified by
  grep: `SiteCatalog` is threaded explicitly through `IngestService(sites=...)`,
  `parse_site_code(name, catalog)` and `default_system_rules(catalog)`, exactly as
  CLAUDE.md claims. That check passed cleanly.

---

## 4. Questions for the user

**Q1. Are `preview()` and the write-time validators dead for good, or parked?**
This is the highest-leverage question in the audit, because D2, D3, C2, P3, S3 and
T3 all resolve differently depending on the answer. Both `ClassificationService.
preview` and `HealthPolicyService.preview`, plus `validate_rule_write`,
`validate_policy_write`, `validate_system_field_lock` and
`_validate_scope_source_coherence`, became unreachable from production code when the
write endpoints were removed (`27b20a8`, `f9ab059`) — they survive only in
`tests/unit/application/services/`. That is roughly 250 lines of application code
and two test modules asserting behaviour nothing can invoke. No ADR covers the
removal's intended end state. If rules and policies are read-only permanently,
deleting them is the largest single simplification available here. If they are
parked for a future authoring UI, they should say so in a comment so the next
audit does not re-raise this.

**Q2. Is C4's unenforced condition invariant acceptable, given Q1?**
`validate_condition`'s docstring is explicit that evaluation *trusts* a condition
that passed validation once — a reasonable contract when writes existed. With the
write path gone, nothing validates stored conditions ever, and the only thing
standing between a bad stored document and a per-server `TypeError` during ingest is
that the documents happen to be seeded from code. That is true today. Is it worth
the ~15 lines to make it structurally true, or is "the defaults are the only
policies that exist" a guarantee you are comfortable relying on?

**Q3. Should `/health/ready` return an RFC 9457 body on 503?**
It is the only endpoint that bypasses the envelope, and there is a real argument it
should: it is consumed by orchestrator probes, not API clients, and its own module
docstring makes exactly that "not an API client" distinction for why it is
unversioned. But ADR-0002 does not carve out an exception, so the deviation is
currently indistinguishable from an oversight. Deliberate, or worth changing?

**Q4. Should filter *values* be validated against their enums?**
`?vendor=nonsense` currently returns an empty page. Keys are strictly whitelisted,
so this is not an injection risk — the question is purely whether an unmatchable
value should be a 400 (`UNKNOWN_FILTER`-style) or an honest empty result. The facets
endpoint (`servers.py:255`) arguably makes the current behaviour better: the UI
learns which values are selectable rather than guessing. Leaving as-is seems right,
but it is a deliberate choice worth recording.

**Q5. Should `search_tokens`, `name_normalized` and `model_normalized` be in
`ServerDetail`?** `schemas.py:14-23` says the detail schema exists precisely as a
seam to ask "should this be public?" of every storage field. These three are pure
index-support fields with no client use, and `search_tokens` in particular is a list
of up to 64 strings on every detail response. They may be there deliberately for
debugging; if so, one line in the docstring settles it.

**Q6. Is optimistic concurrency (C6) in scope for this pass?** `RevisionConflictError`,
`ErrorCode.REVISION_CONFLICT` and the `revision` field all exist and are wired to
nothing — the envelope even documents a `409` example using it. That looks like
scaffolding built ahead of a slice that has not landed rather than an oversight, but
CLAUDE.md's not-done list does not mention it. Given that a collector run and an
operator's reclassify can genuinely race on the same document, is closing this part
of "production and really run", or a later slice?

---

## Method

Docstring numbers come from an `ast` walk over every file in scope, classifying each
`ClassDef`/`FunctionDef`/`AsyncFunctionDef` as `google` (docstring present and
carrying every section it needs), `partial` (docstring present, a required section
missing) or `none`. "Required" is computed per item: `Args:` when the item takes
arguments other than `self`/`cls`; `Yields:` when the body yields; `Returns:` when
it has a non-`None` return annotation or returns a value. Comment-density figures
come from `tokenize`. Dead-code claims were each verified by grep across `backend/`,
`tests/` and `tools/` — a symbol is called dead only when its sole occurrences are
its own definition and other dead code.
