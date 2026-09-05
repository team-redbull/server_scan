# Handoff — Phase 1 (provider ABC) done; here's what's next

Written 2026-09-06, end of the session that implemented Phase 1 of
`docs/notes/2026-09-refactor-plan.md`. Read that plan and
`docs/notes/2026-09-audit.md` first if you weren't in this session — this
file only covers what changed since the plan was approved and what is
still open.

## What is done, committed, and verified

Phase 1 — the provider ABC contract — is implemented, tested, and green:

- `backend/app/domain/ports/provider.py`: `ServerInventoryProvider` is now
  an `ABC` with `collect()` (template method), `collection_errors`
  (inherited property), `_record_error` (inherited mutator), and two
  abstract methods (`health_check`, `_list_servers`).
- All seven providers (`fake`, `ucs_manager`, `ucs_central`, `intersight`,
  `openmanage`, `oneview`, `redfish`) migrated: `list_servers` renamed to
  `_list_servers`, hand-rolled `collection_errors` bookkeeping deleted
  where it existed, `super().__init__()` added.
- **OneView's motivating bug is fixed**: it never had `collection_errors`
  before this. It does now, wired through a new `OneViewClient.truncations`
  list that the truncation-detection code in `get_all` already had the
  data for. A truncated OneView run now exits 3 (PARTIAL) instead of 0 —
  this is a real, intentional behaviour change for that one collector.
- `tools/run_collector.py`: `collection_errors_of` (the reflective
  `getattr` helper) is deleted, along with its copy in
  `openmanage/provider.py`. `_NameFilteredProvider` inherits the ABC and
  overrides `collection_errors` to delegate to the wrapped provider.
- Full detail, the ABC-vs-Protocol reversal and why, and the per-provider
  table: `docs/adr/0023-provider-abc-contract.md`.

**Gate, run at the end of this session, all green:**

```
uv run pytest -q                                                    # 1037 passed
uv run ruff check .                                                 # All checks passed!
uv run ruff format --check .                                        # 213 files already formatted
uv run ty check backend/app tools                                   # All checks passed!
```

Frontend was **not touched** in Phase 1 — no frontend gate was re-run,
since nothing there changed.

Dev stack: started via `scripts/dev-up.sh up` this session (Docker's
daemon wasn't running; used the podman-pod fallback). **Not yet torn
down** — do that (`scripts/dev-up.sh down`) once you've confirmed you
don't need it for the next phase's baseline.

## What is explicitly deferred, not forgotten

**Extend `ty check` to `tests/`.** Today the gate only checks
`backend/app tools`. While fixing test breakage from the ABC migration,
several pre-existing `# type: ignore[method-assign]` / `[arg-type]`
comments turned up in test files (`test_ucs_manager_provider.py:172`,
`test_openmanage_provider.py:207,224`, `test_ingest_partial_reads.py:92`,
`test_ingest_gpu_catalog.py:75`, and likely more not yet surfaced because
`tests/` isn't checked at all). Per `CLAUDE.md`'s own documented ty
gotcha, **ty honours a bare `# type: ignore` but not a coded one** — so
every one of these coded suppressions is currently suppressing nothing,
silently. This was flagged in the original Phase 1 research as "the
strongest single finding of this round" and is still true. It's a
real, separately-scoped pass: run `uv run ty check tests`, expect a
non-trivial batch of diagnostics, and fix each by either correcting the
bare ignore or restructuring the test double so the mismatch is real
rather than suppressed. Don't fold it into a phase that's touching
something else — it deserves its own commit and its own review.

## Everything else in the plan is unchanged

Phases 2 through 11 in `docs/notes/2026-09-refactor-plan.md` are exactly
as approved and have not been started. In order:

2. Concurrency/lifecycle fixes (singleflight cancellation, UCS Central
   task cancellation, the executor-shutdown stall — Q3's decision).
3. Security/supply chain (cursor secret, `cryptography` bump, Prometheus
   label cardinality).
4. API/domain correctness (bootstrap validation — Q1's decision — plus
   deleting the dead preview path, optimistic concurrency for Q4).
5. Performance (measured only — the ~20k-uncached-reads-per-run fix).
6. Frontend correctness (the "all healthy" bug, `NetworkTab`'s missing
   `unread_fields` handling, the silent maintenance-write failure).
7. Operator UX (UX-1, UX-2, UX-4 — approved; UX-3 held).
8. Tooling/CI (`import-linter`, new ruff rule selections).
9. Test gaps (`tests/api/` conftest, the untested `_UNFILTERED_TYPES`
   exemption).
10. Docstrings (the big mechanical sweep — 244 of 363 items in
    domain/API/application currently have none at all).
11. Documentation truth pass (18 known-false statements in the docs).

Every decision in the "Decisions taken" table at the top of the plan
(Q1–Q7) still holds and does not need re-litigating.

## The exact prompt for tomorrow's session

Paste this verbatim to start the next session:

> Continue the production-hardening pass on this repo. Read
> `docs/notes/2026-09-refactor-plan.md` in full, then
> `docs/notes/2026-09-handoff-phase1-followups.md` for what changed since
> it was approved. Phase 1 (the provider ABC contract, ADR-0023) is done,
> committed, and the gate is green — verify that's still true
> (`uv run pytest -q && uv run ruff check . && uv run ruff format --check
> . && uv run ty check backend/app tools`) before touching anything, and
> `podman ps`/`docker ps` before assuming the dev stack's state.
>
> First do the deferred item from Phase 1: extend `ty check` to `tests/`
> and fix the batch of coded `# type: ignore[...]` suppressions it
> surfaces (ty only honours a bare `# type: ignore`) — this is its own
> commit, not folded into anything else. Then proceed to Phase 2
> (concurrency/lifecycle fixes) in the plan, in order. Use plan mode for
> anything touching more than a few files, hand the design to Codex
> before implementing anything non-trivial, and confirm with me before
> any decision the plan doesn't already settle.

## Commit and push status

This session's Phase 1 work is committed on `dev-refactor` (branched from
`dev-tomer`) as `feat!: require every collector to report
collection_errors and release its session` (see the commit for the exact
message and body). Author is `TomerKarniol <tomer.karniol@gmail.com>`,
no `Co-Authored-By` trailer, no generated-with footer — verify with
`git log --format="%an <%ae>" -1` if anything looks off. The branch has
been pushed to origin.
