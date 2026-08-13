# ADR-0008: Playwright E2E suite; the maintenance toggle finally got a UI

## Status

Accepted

## Context

Phase 1's remaining slices are the 10k/50k performance pass (slice 6,
done — ADR-0007) and E2E coverage of the critical admin flows (slice 7,
this ADR), before real authentication lands last (slice 8, the release
gate).

While building the E2E suite, two things surfaced that this ADR records
because both are the kind of gap that's easy to miss without actually
driving the app end to end in a real browser rather than trusting
unit/component tests alone:

### 1. Maintenance had a backend but no UI

`PUT`/`DELETE /api/v1/servers/{id}/maintenance` (slice 4) were fully
built, audited, and tested at the API layer — but no frontend ever called
them. `OverviewTab` rendered the maintenance state read-only. This wasn't
caught by any prior slice's review because maintenance was never
in-scope for slices 1 or 5 (inventory table and classification/health
editors, respectively) — it fell into the gap between them. Writing an
E2E test for "the critical maintenance flow" is what surfaced that there
was no flow to test.

Fixed by adding `enableMaintenance`/`disableMaintenance` to
`app/api/servers.ts`, `useEnableMaintenanceMutation`/
`useDisableMaintenanceMutation` to `app/features/servers/hooks.ts`, and a
small inline form/button in `OverviewTab` (a reason input + "Start
maintenance", or "End maintenance" when already enabled) — kept as
optional props (`onEnableMaintenance`/`onDisableMaintenance`) rather than
required, so `OverviewTab` stays usable in a read-only context if a future
caller doesn't need to own the mutation.

### 2. A real browser accessible-name quirk broke the obvious test selector

Every `getByLabel("X")` locator against this app's `<label>Text<select>…
<option>…</option>…</select></label>` fields (Source, Vendor, Field, Name,
policy_key — every top-level form field in the classification-rule and
health-policy editors) is fragile in a way that only shows up when the
test actually runs in a real browser: Chromium computes both the
accessible name *and* `textContent` of a label wrapping a `<select>` as
the label's own text concatenated with **every `<option>`'s text**. The
"Source" field's real computed value is
`"SourceSITE_CUSTOMMANAGER_CUSTOMVENDOR_CUSTOMGLOBAL_CUSTOM"` — which
contains `"Vendor"` (case-insensitively, inside `...MANAGER_CUSTOM
VENDOR_CUSTOM...`) and `"Name"` (inside `CLASSIFIABLE_FIELDS`'s own
`"name"` option on the *Field* selector). `page.getByLabel("Vendor")` and
`page.getByLabel("Name")` both intermittently resolved to the *wrong*
control — a strict-mode collision Playwright, to its credit, refuses to
silently guess through.

`{ exact: true }` doesn't fix it either — it just means `getByLabel("Source")`
matches *nothing*, since the field's real accessible name is never the
bare word "Source" at all.

Fixed with a small helper, `labeledField(page, text)` in `e2e/helpers.ts`,
using an XPath `text()` axis (`//label[normalize-space(text())="X"]`)
instead of `getByLabel`/`locator("label", {hasText})` — `text()` selects
only a label's *direct* child text node, not descendant text inside the
nested `<select>`/`<option>` elements, so it isn't subject to the
concatenation at all. `ConditionBuilder`'s fields (Metric/Operator/Value)
don't have this problem — they use a real `aria-label` attribute directly
on the control, which is an explicit override, not a computed value, so
plain `getByLabel` works fine there and was left alone.

This is worth a real ADR (not just a code comment) because it will bite
again the moment a new form field is added to either editor page and a
future E2E test reaches for the obvious `getByLabel` — the fix pattern
(`labeledField`, not `getByLabel`, for anything wrapping a `<select>`
with option text) needs to be discoverable, not just present once.

## Decision

- `frontend/e2e/` holds the critical-flow Playwright suite: inventory
  (list/search/detail/tab-switching), classification rules (create,
  live preview, disable, delete), health policies (create two sharing a
  `policy_key`, see the shadow panel, delete), and maintenance
  (enable/disable from the detail page, reflected in the inventory
  filter).
- Targets `vite dev` (not a `vite preview` build) — see
  `playwright.config.ts`'s docstring: `preview` needs its own
  `preview.proxy` config distinct from the dev server's `server.proxy`,
  and testing against the exact server whose proxy behavior is already
  production-verified (the `/health-policies` route-collision fix from
  slice 5) is worth more than testing against a minified bundle here; the
  production build itself is still separately checked by the `frontend`
  CI job's `npm run build` step.
- Test-created classification rules/policies use a per-run unique name
  (`uniqueName()`) and clean up via a direct API call in
  `test.afterEach` — verified idempotent by running the suite three times
  back to back with zero leftover `e2e-*`-named resources.
- New `e2e` job in CI: spins up mongo+redis, starts the real backend,
  seeds a small deterministic dataset (300 servers — this is flow
  coverage, not the 10k/50k scale check slice 6 already owns), starts the
  frontend dev server, runs the suite, and uploads the HTML report as an
  artifact on failure.
- `vite.config.ts`'s Vitest `test.exclude` now excludes `e2e/**` — without
  it, Vitest's default glob picks up Playwright's own `*.spec.ts` files
  and fails them with "did not expect test.describe() to be called here"
  (two different test runners both claiming the same files).

## Consequences

- Any new field added to the classification-rule or health-policy editor
  forms that wraps a `<select>` must be located with `labeledField`, not
  `getByLabel`, in any future E2E test — this isn't optional, it's a
  correctness requirement given the confirmed browser behavior above.
- Chromium (not `--with-deps`-installed) requires several system shared
  libraries (`libnspr4`, `libnss3`, `libasound2`, and others) that aren't
  present by default outside a container with the full Playwright image
  or a CI runner with root access. In this dev sandbox specifically
  (no passwordless `sudo`), the browser was made to run by downloading
  the needed `.deb` packages with `apt-get download` (no root required —
  it only fetches, doesn't install) and extracting them into a local
  prefix added to `LD_LIBRARY_PATH`. That workaround is local-environment-
  specific and not committed anywhere; CI's `npx playwright install
  --with-deps chromium` step handles it properly on a real runner with
  root, and any contributor with `sudo` locally should just run that same
  command directly.
