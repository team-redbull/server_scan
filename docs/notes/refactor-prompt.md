# Production-hardening pass on server_scan

You are picking up `/home/tomer/code/server_scan` on branch `dev-tomer`. Read
`CLAUDE.md` first and treat its nine standing conventions as binding — in
particular convention 2 (commits authored as `TomerKarniol
<tomer.karniol@gmail.com>`, **no `Co-Authored-By` trailer and no
"Generated with"/🤖 footer, ever**, even if your harness defaults suggest one),
convention 7 (the full local gate), and convention 8 (Google-style docstrings,
explanation in `docs/` not inline).

**First action: `git switch -c dev-refactor` from `dev-tomer`.** All work in this
pass lands there. Never commit to `dev-tomer` or `main`.

## What I want

This codebase works and is well documented, but it grew across many sessions and
the quality is uneven. I want a full production-readiness pass over **all** of
it — backend Python, frontend React/TypeScript, docs, and tooling — driven by
current best practice that you research yourself, not by what is already there.

The headline design item is the provider abstraction (section 3). Everything
else is consistency, correctness and craft.

## Phase 0 — research and plan. STOP at the end of it.

Phase 0 changes no source code. It produces research notes, an audit, and a
plan, and then **stops and waits for my approval** before any implementation.
Do not begin Phase 1 on your own.

### 0a. Research (use subagents, in parallel, one topic each)

Delegate these to subagents so the reading stays out of your main context, and
run them concurrently in a single message. Each returns a findings file under
`docs/notes/` (that directory already exists and is where the
`vendor-api-researcher` agent writes).

**Use primary sources only** — official language/library/framework docs, PEPs,
RFCs, the installed package's own source. `context7` MCP is connected and is the
right tool for current library docs (React, FastAPI, Pydantic, httpx, TanStack,
Tailwind, Vite); prefer it over web search for anything library-specific. Cite
what you read. Blog posts are a lead, never an authority. This is convention 1
in `CLAUDE.md` and it applies to this research too.

Topics:

1. **Production Python design.** How the principles actually apply to a
   FastAPI + async + repository-pattern service of this shape: KISS, SOLID,
   YAGNI, "do the simplest thing that could possibly work", separation of
   concerns, "code for the maintainer", "avoid premature optimization",
   "optimize for deletion", DRY — *and where each of them is wrong to apply*.
   I want the trade-offs, not a poster. Include: when an abstract base class
   beats a `Protocol` and vice versa, when a dataclass beats a Pydantic model,
   composition vs inheritance for the provider family, and what "one function,
   one reason to change" means for a 700-line vendor mapping module.
2. **Logging.** Correct use of `structlog` in an async service: bound loggers,
   context vars across a request/collection run, log levels that mean something,
   what belongs at INFO vs DEBUG vs WARNING vs ERROR, cardinality of structured
   fields, exception logging (`exc_info` vs `format_exc_info`), and how logging
   interacts with the Prometheus metrics this repo already exposes. Compare
   against `backend/app/infrastructure/logging/config.py`, which is good, and
   against the actual log call sites, which are not uniformly good.
3. **Sync vs async, and the HTTP client question.** When `async def` earns its
   keep and when it is cargo cult; `asyncio.to_thread` vs a real thread pool for
   blocking SDKs (`ucsmsdk` is synchronous — see
   `app.infrastructure.providers.ucs_manager.client`); `asyncio.gather` vs
   `TaskGroup` vs bounded `Semaphore` fan-out; cancellation and timeout
   semantics; connection-pool lifetime and reuse. Settle **`httpx` vs
   `requests`** on current merit for this project (this repo already uses
   `httpx` in four vendor clients — confirm that is right, and confirm the
   client construction, limits, timeouts, retries and `verify` handling are).
4. **Decorators.** What decorators are for, how to write them correctly
   (`functools.wraps`, typing with `ParamSpec`/`TypeVar`, async-aware
   decorators, stacking order, when a decorator is worse than a plain call), and
   then — concretely — **where this project would genuinely benefit**. The
   current decorator census is: `@dataclass` ×40, `@property` ×20,
   `@router.*` ×15, `@classmethod` ×10, `@staticmethod` ×6, `@lru_cache` ×4,
   `@model_validator`/`@field_validator` ×4, `@asynccontextmanager` ×1. There
   are no project-authored decorators at all. Candidates worth evaluating
   honestly: retry/backoff on vendor calls, timing/instrumentation, per-run
   error capture, cache-aside around Redis reads, deprecation markers. For each,
   say whether a decorator is actually better than the explicit code, and say so
   plainly when it is not — I do not want decorators added for their own sake.
5. **Performance without complexity.** How to find real bottlenecks rather than
   guess: profiling an async service (`py-spy`, `scalene`, `cProfile` +
   `asyncio` debug mode, `pytest-benchmark`), finding event-loop blocking,
   MongoDB query/index analysis (`explain`), Redis round-trip batching, and
   FastAPI response-serialisation cost. The target scale is ~10,000 servers with
   headroom to 50,000 and is a real requirement — see ADR-0007. **Optimise only
   what you measure**, and never at the cost of code a maintainer can read.
6. **Frontend.** Current best practice for React 19 + TypeScript 7 + Vite 8 +
   Tailwind 4 + TanStack Query 5 + TanStack Table 9 + react-router 8, which is
   exactly this stack (`frontend/package.json`). Cover: component and directory
   structure, when a component should split, server-state vs client-state,
   query keys and cache invalidation, suspense and error boundaries, loading and
   empty states, list virtualisation for a 10k-row inventory table, form and
   filter patterns, accessibility basics (keyboard, focus, ARIA, contrast),
   Tailwind 4 conventions (`@theme`, design tokens, avoiding class soup),
   bundle size and code splitting, and what `oxlint` + `tsgolint` can and cannot
   catch.
7. **Tooling.** What linting, type-checking, complexity, dead-code, security and
   profiling tools are worth adding to a repo that already runs `ruff` (a
   specific selected rule set), `ty` 0.0.76, `pytest`, `oxlint`, `vitest` and
   Playwright. Evaluate at least: `ruff` rules not currently selected (`PERF`,
   `RET`, `PTH`, `TRY`, `LOG`, `D` for docstring style, `C90` complexity),
   `vulture`/`deadcode`, `radon`/`xenon`, `bandit` vs ruff's `S`,
   `import-linter` for layering, `pip-audit`, `npm audit`, bundle analysis. For
   each: what it catches that nothing else does, its false-positive rate, and
   whether it should gate CI or just be a local command. **Do not add a tool
   whose findings you would then ignore.**

### 0b. Audit the repository against that research

Read all of it — `backend/app` (~14k lines), `tools` (~4k), `frontend/src`
(~5k), `tests`, `deploy`, `.github/workflows`. Use subagents for the fan-out.

Known problems to confirm, quantify and add to:

- **Docstrings are inconsistent.** 40 of the 94 Python files containing function
  definitions have Google-style `Args:` sections; ~54 do not. The whole
  `app/domain/` and `app/api/` layer is in the older style. Convention 8 is the
  target shape.
- **Explanation lives in the wrong place.** Several modules open with 20+ line
  prose docstrings and carry multi-paragraph justifications between statements.
  Per convention 8 a function's docstring is a short summary of **what the
  function does**, followed by the `Args:`/`Returns:`/`Raises:`/`Yields:`
  sections. See "The length rule" below for what is and is not capped. The
  reasoning moves to `docs/` (an ADR for a
  decision, a `docs/<vendor>-collectors.md` for a verified fact). **Never delete
  a hard-won fact** — facts like "UCSPE 4.2 reports `access='unspecified'` on a
  blade's own `mgmtIf`" cost a live-hardware run; move them to `docs/` with
  their provenance intact.
- **The provider contract is incomplete** — section 3 below.
- Anything else you find: dead code, duplicated logic, functions doing three
  things, silent failure paths, missing input validation at boundaries,
  inconsistent error handling, N+1 queries, unnecessary `async`, blocking calls
  on the event loop, over-broad `except`, unused dependencies.

Write `docs/notes/2026-09-audit.md`: every finding with file:line, severity
(correctness / maintainability / performance / consistency), the fix, and the
effort. Rank it. **Be honest about what is already good** — this repo has real
strengths (the carry-forward `None` semantics, keyset pagination, the
`policy_key` shadowing design, structlog config, the ADR discipline) and a
finding list that pretends otherwise is noise.

### 0c. Write the plan

`docs/notes/2026-09-refactor-plan.md`: the phases, what each changes, in what
order, what could break, and how each is verified. One phase = one reviewable
commit. Order by risk: contract/architecture first while the diff is legible,
mechanical sweeps last.

**Then stop and show me the plan.** Do not start Phase 1.

## The work itself (Phases 1..N, after I approve)

### 1. The provider abstraction — the headline item

Today `app.domain.ports.provider.ServerInventoryProvider` is a `Protocol` with
exactly three members:

```python
class ServerInventoryProvider(Protocol):
    provider_type: str
    async def health_check(self) -> None: ...
    def list_servers(self) -> AsyncIterator[ProviderServer]: ...
```

That is far too thin for seven implementations. Everything else each provider
must do is informal and rediscovered per vendor:

- **Connect/authenticate.** Every provider has a private `_new_client()` and its
  own login handling. Intersight signs requests instead (no login at all),
  OpenManage needs *two* logins (OME + iDRAC), UCS Central logs into each
  discovered domain. None of that is expressed in the contract.
- **Disconnect/logout.** No provider declares one. `tools/verify_oneview.py`
  logs out explicitly because a OneView session left open is a real resource.
- **`collection_errors`.** Implemented by `intersight`, `redfish`, `openmanage`
  and `ucs_central`; absent from `fake` and `ucs_manager`. `tools/run_collector.py`
  reaches it through a duck-typing helper, `collection_errors_of(provider)` at
  line 517, precisely because the contract does not promise it. That helper is
  the smell.
- **Listing names vs fetching one server's data.** Some providers naturally
  split discovery from detail (OpenManage: OME says which servers exist, iDRAC
  says what they are; UCS Central: enumerate domains, then collect each). Others
  do it in one pass. Nothing names the distinction.

**Build a real abstract base class** — `ABC` with `@abstractmethod`s — that
states the complete lifecycle every provider must implement, and migrate all
seven (`fake`, `ucs_manager`, `ucs_central`, `intersight`, `openmanage`,
`oneview`, `redfish`) onto it. I want it obvious, from one file, exactly what a
new vendor has to write.

Requirements:

- Name the full lifecycle: connect/authenticate, health check, list server
  identities, fetch one server's data, report collection errors, disconnect.
  Use a template method for the parts that are genuinely identical across
  vendors, and abstract hooks for the parts that differ.
- The base class carries the shared behaviour that is currently copy-pasted:
  error accumulation, per-run counters, the "`None` means could not read, never
  zero" rule, structured logging of a run's start/end/summary. A provider
  subclass should be mostly vendor mapping.
- It must be an **async context manager**, so connect/disconnect are guaranteed
  and a leaked vendor session becomes impossible.
- It must not force a shape onto a vendor that does not have it. Intersight has
  no login; UCS Central fans out to N domains; OneView must stay OneView-only
  for every iLO generation (ADR-0022 — that decision is **not** open for
  re-litigation, no Redfish pass, no BMC credentials). If a hook does not apply,
  the base class must let a provider say so cleanly, not force a lying stub.
- Keep `ProviderServer` as the DTO. Keep `CredentialResolver` as a `Protocol` —
  it is a seam for a future secret store and inheritance would be wrong there.
- Preserve every behaviour ADRs 0009, 0014, 0016, 0017, 0020, 0021, 0022 record.
  The `IngestService` correlation key, the carry-forward semantics, the
  `unread_fields` recomputation, the name-pattern filter and its
  `REDFISH_STANDALONE` exemption, and the collector exit codes must all still
  hold. **The vendor mapping logic is hard-won and mostly correct — you are
  restructuring the contract around it, not rewriting the mappings.**

If, having researched it, you conclude a `Protocol` plus a shared mixin (or some
other shape) is genuinely better than an ABC, **say so, argue it with evidence,
and implement that instead** — but the goal is non-negotiable: one explicit,
discoverable, type-checked contract that a new vendor implements by filling in
named methods.

Write an ADR (next free number after 0022) covering the decision, the
alternatives, what changed per provider, and the migration.

### 2. Backend code quality

Apply the research. Concretely:

- Every function, method and class gets a Google-style docstring in the shape
  convention 8 specifies. Summary line, then `Args:` (each with its type in
  parentheses, skipping `self`), `Returns:`, `Raises:`, `Yields:` as they apply.
  A no-arg no-return function still gets the summary line.

  **The length rule, precisely.** The cap is on the *summary* — the prose that
  says what the function does. That is **two or three lines, maximum**. It is
  **not** a cap on the docstring: `Args:`, `Returns:`, `Raises:` and `Yields:`
  are structured reference, they take as many lines as there are arguments and
  outcomes, and they are never trimmed to hit a line count. Document every
  argument and every raised exception, however long that makes the block.

  What the cap forbids is the *essay* — background, history, why the design is
  this way, what was tried before, what a vendor's API does. That goes to
  `docs/`. Concretely:

  ```python
  def resolve(self, manager_type: ManagerType) -> ManagerConnection:
      """
      Look up the connection details configured for a manager type.

      Args:
          manager_type (ManagerType): The vendor manager to resolve.

      Returns:
          ManagerConnection: Endpoint, username and password for that type.

      Raises:
          ManagerNotConfiguredError: If any of the three values is missing.
      """
  ```

  Three lines of summary, then as much `Args:`/`Returns:`/`Raises:` as the
  signature needs. *Why* a partially-populated connection is never returned is
  an ADR, not a paragraph in this docstring.
- Prune the inline comment walls. A couple of one-line `#` pins per file for
  genuinely non-obvious behaviour; the reasoning moves to `docs/`.
  `# ponytail:` markers are tracked debt and stay.
- Fix what the audit found: separation of concerns, functions with one job,
  duplicated logic, dead code, over-broad exception handling, missing validation
  at trust boundaries, unnecessary `async`, blocking calls on the loop.
- Logging: consistent levels, structured fields, bound context through a
  collection run, no secrets (the `_drop_sensitive_keys` processor is the last
  line of defence, not the first).
- Performance: **profile before changing anything.** Include the numbers in the
  commit body. If a change does not show up in a measurement, do not make it.
  `tools/loadtest.py` and ADR-0007 are the existing baseline.
- Do not add abstractions with one implementation, config for values that never
  change, or scaffolding for a feature nobody asked for. Deletion is a valid and
  preferred outcome.

### 3. Frontend

Same standard for `frontend/src`. Apply the research: component structure and
splitting, TanStack Query patterns and cache invalidation, loading/empty/error
states, table performance for 10k rows, Tailwind 4 tokens over class soup,
accessibility, TypeScript strictness, bundle size.

And **the interface itself**: this is an operator's tool for finding a server
and understanding why it is unhealthy. Look at `InventoryPage`, `SitesOverviewPage`,
`ServerDetailPage` and its tabs and say what would actually make an operator's
job faster — keyboard navigation, filter discoverability, what the empty and
error states say, whether `unread_fields` is legible as "not read this run"
rather than as a confident zero, whether health severity is scannable at a
glance. Propose it in the plan; build what I approve.

`frontend/e2e` gotcha, still live: never use `getByLabel` on a page with sibling
`<select>` elements — a Chromium quirk folds every `<option>`'s text into the
label's computed name (ADR-0008). No page has a `<select>` today; the next form
page will hit it.

### 4. Docs

Every decision made in this pass gets an ADR. Update `CLAUDE.md`,
`docs/architecture.md`, `docs/arc42.md` and the affected `docs/*-collectors.md`
so they describe the code as it now stands. **A doc that describes the old shape
is worse than no doc.** Release notes come from commit subjects (convention 9),
so write subjects an operator can act on — there is no `CHANGELOG.md` to update.

### 5. Tooling and CI

Add the tools the research justified. Wire the ones worth gating into
`.github/workflows/ci.yml`, keep the rest as documented local commands. Do the
standing "Keeping CI current" chore in `CLAUDE.md` while you are in there: pin
freshness, `pip-audit`, `npm audit`, unused dependencies, Node-runtime
deprecations, the UBI base image, and the `ty` version — and remember a new `ty`
diagnostic after a bump is ty changing, not a regression here.

## How to work

- **Use the tools you have, deliberately.** `Explore` and `general-purpose`
  subagents for fan-out reading and for the seven research topics — in parallel,
  in one message. `Plan` for the provider redesign. The project-local
  `vendor-api-researcher` agent (`.claude/agents/`) for anything that needs a
  vendor API confirmed from primary sources. `context7` MCP for current library
  docs. `chrome-devtools` MCP for real frontend performance traces, Lighthouse
  and console/network inspection against the running dev server — do not guess
  at frontend performance when you can measure it.
- **Use the language servers — they are installed and they are the right tool
  for this refactor.** `ty-lsp` (Python, backed by `uv run ty server`, so its
  diagnostics are the *same* checker CI gates on) and `typescript-lsp`
  (frontend). Both push diagnostics into your context after every edit: if you
  break a type, you see it in the same turn rather than at the next `ty check`.
  More importantly for Phase 1, use **find-references and call-hierarchy** — not
  grep — before changing any provider method, `ProviderServer` field or exported
  symbol. Renaming a member of a contract seven providers implement is exactly
  the case where a grep miss becomes a silent break. Rely on go-to-definition,
  find-implementations and rename over text search wherever the LSP can answer.
- **Relevant installed skills** (exact names — invoke them, don't reinvent
  their checklists): `python-library-complete:improving-python-code-quality`,
  `:testing-python-libraries`, `:optimizing-python-performance`,
  `:designing-python-apis`, `:documenting-python-libraries`,
  `:auditing-python-security`, `:building-python-web-apps`,
  `:reviewing-python-libraries`, `:keeping-git-repos-clean`,
  `:running-github-actions-efficiently`, `:reproducing-ci-locally`,
  `:verifying-external-behavior`, `:shipping-across-surfaces`,
  `:guarding-destructive-operations`. Also `/code-review` and
  `pr-review-toolkit` for the per-phase review, `security-guidance` (already
  active) for anything touching a trust boundary, and `frontend-design` for the
  UI work. Check the skills list yourself at the start and use what fits — I
  will not name them again.
- **Use Codex as a second opinion, not a rubber stamp.** The `openai-codex`
  plugin is installed and `codex:codex-rescue` is available. Hand it: the
  provider ABC design before you implement it, the largest diff of each phase,
  and anything you are stuck on for more than two attempts. Report where Codex
  disagreed with you and who was right. A review that finds nothing on a
  thousand-line diff is a review that did not happen.
- **Adversarial review before each phase is done.** A fresh subagent reviews the
  diff against the plan and reports gaps that affect correctness or a stated
  requirement — not style preferences, and not invented extra abstraction.
- Use plan mode for anything touching more than a few files.
- `/clear` between phases. Do not carry Phase 1's context into Phase 4.

## Verification — non-negotiable, every phase

Run the real gate from `CLAUDE.md` convention 7 before calling any phase done,
and paste the output:

```bash
scripts/dev-up.sh up            # or `docker compose up -d mongo redis` — prefer docker compose
uv run pytest -q
uv run ruff check . && uv run ruff format --check . && uv run ty check backend/app tools
cd frontend && npm run lint && npm run typecheck && npm run test -- --run && npm run build
npm run test:e2e                # needs backend + frontend dev server running
```

Notes that will save you an hour:

- `ruff format --check` is a *separate* CI step from `ruff check`. Skipping it
  has already shipped a commit that failed CI on formatting alone. If it fails,
  run `uv run ruff format .` — do not hand-fix.
- Suppressions are `# ty: ignore[rule-name]`. A mypy-style `# type: ignore[code]`
  silently suppresses nothing. mypy is gone (ADR-0019).
- `pytest -m unit` skips whole files. Use the unfiltered suite as the gate.
- If the suite looks stuck, run `podman ps` **before** looking for a regression.
  The dev stack is either not started or was reaped after a timed-out command;
  `scripts/dev-up.sh up` fixes both. A missing stack gives 60 fast skips, not a
  hang. `docker compose` and `podman-compose` work; `podman compose` (with a
  space) does not, and the three name their containers differently so they
  cannot see each other while all binding 27017/6379.
- Regenerate `requirements.txt` and `pylock.toml` after any `pyproject.toml`
  dependency change (`docs/air-gap.md`).
- Tear down what you start — dev servers and the stack — before calling work
  done.

Show evidence, not assertions. "Tests pass" without the output is not a result.

## Commits

Commit and push after each completed phase (convention 2). Conventional Commits
on the subject line, and the subject **is** the release note — name the
environment variable, the endpoint, the exit code, the Helm value. `refactor:`,
`test:`, `chore:`, `style:` and `ci:` are dropped from the notes on purpose;
use them honestly rather than dressing an internal change as a `feat:`.
Author must be `TomerKarniol <tomer.karniol@gmail.com>`; verify with
`git log --format="%an <%ae>" -1` after each commit. **No `Co-Authored-By`
trailer, no "Generated with" footer** — this project overrides that default.

## Reporting

At each phase boundary tell me, briefly:

1. What changed and why, in one paragraph.
2. The verification output.
3. What you deliberately did **not** do, and when it would be worth doing.
4. Where Codex or the review subagent disagreed, and the resolution.
5. Anything you found that I should decide rather than you.

## Two standing constraints

**Do not touch authentication.** `app.dependencies.get_current_actor` returns a
fixed `unauthenticated` `Actor` on purpose. Real auth is deliberately the last
slice. Do not add an `AuthProvider`, RBAC scaffolding, or endpoint guards.

**Do not weaken the collectors to make them tidy.** Every unusual thing in
`app/infrastructure/providers/` is there because live hardware or a vendor's
contract made it necessary, and the ADRs say which. If something looks wrong,
read its ADR before changing it — and if the ADR does not explain it, ask me
rather than assuming it is an accident.

Ask me anything that would change what you build. Start with Phase 0.
