# Frontend research — React 19 / TanStack / Tailwind 4 / Vite 8

Research note for the production-hardening pass, 2026-09-06. Branch
`dev-refactor`. **No source code was changed by this note.**

Everything below is checked against the versions actually installed
(`frontend/package-lock.json`, lockfileVersion 3), not the versions the
brief named. Primary sources only, via the `context7` MCP server against
each project's own documentation repository.

---

## 0. What is actually installed

The brief said "React 19 + TypeScript 7 + Vite 8 + Tailwind 4 + TanStack
Query 5 + TanStack Table 9 + react-router 8". The lockfile agrees on all
seven, and pins them at:

| Package | Installed | Note |
|---|---|---|
| `react` / `react-dom` | 19.2.8 | |
| `typescript` | 7.0.2 | `baseUrl` removed; `paths` alone is used (`tsconfig.app.json:44-47`) |
| `vite` | 8.2.1 | rolldown-based; see §8 |
| `@vitejs/plugin-react` | 6.0.5 | |
| `tailwindcss` / `@tailwindcss/vite` | 4.3.3 | CSS-first, no JS config file |
| `@tanstack/react-query` | 5.101.4 | |
| `@tanstack/react-table` | 9.1.2 | used **only** through `/legacy` |
| `react-router` | 8.3.0 | the `react-router` package, not `react-router-dom` |
| `oxlint` | 1.78.0 | `package.json` says `^1.75.0`; 1.78.0 is what resolved |
| `oxlint-tsgolint` | 7.0.2001 | type-aware backend |
| `vitest` | 4.1.10 | |
| `@playwright/test` | 1.62.1 | |

Not installed, and relevant to §5: **`@tanstack/react-virtual` is not a
dependency.** Nothing in `frontend/src` virtualises anything.

Measured production build, `npx vite build`, 2026-09-06:

```
dist/assets/index-BXv_QRWa.css   27.16 kB │ gzip:   6.20 kB
dist/assets/index-D6rYWdvw.js   478.87 kB │ gzip: 140.59 kB
✓ built in 373ms
```

One JS chunk. No code splitting at all (§8).

---

## 1. Component and directory structure

The current layout is already the right one and does not need a refactor:

```
src/api/        one module per backend resource + client.ts + queryKeys.ts
src/components/ cross-feature presentational primitives
src/features/<domain>/  page + hooks + feature-local components + tests
src/lib/        framework-agnostic helpers
src/types/      hand-written wire types
```

52 files, ~5,600 lines including tests. That is small. The failure mode
for a codebase this size is not "not enough structure", it is
premature splitting — and the repo has mostly avoided it.

**When a component should split**, concretely:

- It renders two things that change for different reasons. `InventoryPage`
  (384 lines) is the filter bar plus the pagination shell plus the data
  wiring; the filter bar is the piece that will change every time a
  filter is added, and it is the piece a test would want to drive alone.
- A file exports both a component and a non-component value, which
  breaks React Fast Refresh — the repo already knows this and documents
  it at `src/components/severity.ts:18-22`, which is why `SEVERITY_GLYPH`
  lives in its own module. That reason is real and is the one structural
  rule worth enforcing mechanically (`react/only-export-components` is
  already on in `.oxlintrc.json:10`).
- A subtree needs its own error or loading boundary. React's docs place
  `Suspense`/error boundaries at the granularity you want to fail
  independently, which is a structural decision, not a styling one
  (react.dev, `use` reference — the Error Boundary example wraps the
  Suspense boundary, not the other way round).

**When splitting is premature**: extracting `SiteCard`, `VendorBar`,
`SkeletonCard`, `SectionHeading` into four files. They already live
inside `SitesOverviewPage.tsx` and that is correct — they have one
caller, they are not independently testable in a way that matters, and
four more files is four more imports for zero reuse. Keep them.

`src/features/events/HistoryPanel.tsx` has **no importers at all** (§9,
F8) — that is the one structural problem: a feature directory that
survived the removal of the pages that used it.

---

## 2. Server state vs client state (TanStack Query 5.101.4)

### What belongs in Query and what does not

Server state is anything whose authority is MongoDB: the server list,
facets, a server detail, sites, rules, policies, events. All of that is
already in Query, correctly, and the key factory in
`src/api/queryKeys.ts` is a good pattern.

Client state is anything the server has no opinion about: which tab is
open, whether a filter bar is expanded, the text currently in a search
box before it is committed. Today the repo puts filter state in the
**URL**, which is the right third category (§7) — but see F10 for the
one place URL-as-state is used for something that should be local state
until it settles.

### Query key design

The factory shape here matches TanStack's own convention: a resource
root, then a narrower scope, then params, all `as const`. The load-bearing
property is that `invalidateQueries({ queryKey })` matches **any prefix**
and defaults `exact: false` — from `queryClient.ts` in the Query source:

> Since `QueryFilters` defaults `exact` to `false`,
> `invalidateQueries({ queryKey: ['posts'] })` matches all queries whose
> key starts with `['posts', ...]`.

So `queryKeys.servers.all` invalidates every server query, and
`queryKeys.servers.lists()` invalidates only the lists. That is exactly
why the factory is worth having.

**The rule this repo breaks (F1): a query key must contain the inputs the
request actually varies on, and nothing else.**
`queryKeys.servers.facets(params)` (`src/api/queryKeys.ts:17-18`) is
handed the full `ServerListParams` including `cursor`, `page_size`,
`sort` and `sort_desc`, while `getServerFacets` (`src/api/servers.ts:53-58`)
deliberately strips all of those before building the URL. The result is a
cache key that changes on every page and sort change for a request whose
URL never changed — a guaranteed miss and a duplicate network round-trip
every time. The comment at `servers.ts:47-51` states the intent
correctly ("sending them would split the cache by page for no reason")
and the key factory then does exactly that.

### `staleTime` / `gcTime` for CronJob-updated data

TanStack's defaults are `staleTime: 0` and `gcTime: 5 * 60 * 1000`, and
the docs are explicit that `staleTime` is the intended lever:

> Stale queries are automatically refetched in the background when new
> instances of the query mount, the window is refocused, or the network
> is reconnected. The `staleTime` option is the recommended way to
> control excessive refetches.
> — *Important Defaults*, TanStack Query

This inventory is written by CronJobs on a schedule and by nothing else
during a browsing session. The data provably cannot change between two
collector runs. `staleTime: 30_000` (`src/lib/query-client.ts:14`) is
therefore an arbitrary number that refetches roughly 20× more often than
anything can change, for a fleet-scale query. The principled setting is
"a fraction of the collection interval", with `gcTime` above it so a
back-navigation inside one interval is free. `refetchOnWindowFocus: false`
(`:16`) is already the right call for the same reason.

The corollary is a UI obligation, not just a config one: if you cache for
minutes, the operator must be able to see *how old* the data is.
`last_seen_at` is on every row and detail (`types/server.ts:100, 296`)
and is rendered in exactly one place (`OverviewTab.tsx:91-94`).

### `placeholderData: keepPreviousData` for paginated tables

Correct as used (`src/features/inventory/hooks.ts:18, 33`). This is the
v5 replacement for v4's `keepPreviousData` boolean and is the right tool
for cursor pagination: the previous page stays on screen while the next
is in flight rather than the table collapsing to a loading state. Both
the list and the facets use it, and the reasoning in the docstrings is
sound.

### Infinite / cursor queries against an opaque HMAC cursor

`useInfiniteQuery` fits this API better than the hand-rolled
`cursorHistory` stack in `InventoryPage`. From the v5 reference:

- `initialPageParam` (required) — the param for the first page.
- `getNextPageParam: (lastPage, allPages, lastPageParam, allPageParams) => TPageParam | undefined | null`
  — return `undefined`/`null` when there is no next page.
- `maxPages` — caps stored pages, **but** the migration guide is explicit:
  "To use `maxPages`, the infinite list must be bi-directional, requiring
  both `getNextPageParam` and `getPreviousPageParam` to be defined."

That last constraint is the one that bites here. The backend gives a
**forward-only** cursor (`PageMeta.next_cursor`, `types/server.ts:105`),
so `getPreviousPageParam` cannot be written, so `maxPages` is unavailable,
so an infinite list on this API grows without bound in memory as the
operator scrolls. That is an acceptable trade for a list an operator
scrolls a few pages into and a bad one for a list they scroll to the end
of — which is the same argument as §5.

The shape, if it is adopted:

```ts
useInfiniteQuery({
  queryKey: queryKeys.servers.list(filterOnlyParams),
  queryFn: ({ pageParam }) => listServers({ ...filterOnlyParams, ...(pageParam ? { cursor: pageParam } : {}) }),
  initialPageParam: undefined as string | undefined,
  getNextPageParam: (last) => last.page.next_cursor ?? undefined,
  placeholderData: keepPreviousData,
})
```

Note the cursor leaves the query key entirely — which is the correct
treatment for an opaque signed cursor, since the cursor is a *position
within* one filter set, not part of its identity.

---

## 3. React 19: Suspense, error boundaries, Actions

What changed in 19 that matters to a read-mostly admin UI, from
react.dev's own 19 release post and API reference:

- **`ref` as a prop.** "In React 19, function components can access `ref`
  directly as a prop, eliminating the need for `forwardRef`." Not
  currently relevant — this codebase has no `forwardRef` and no
  ref-forwarding components at all. It becomes relevant the moment
  anyone builds a shared `<Input>`/`<Button>` primitive; do not reach for
  `forwardRef` when they do.
- **Actions / `useActionState` / `useOptimistic`.** These are form-
  submission and optimistic-update tools. This UI has exactly one
  mutation — maintenance enable/disable (`features/servers/hooks.ts:15-33`)
  — and it is a two-state toggle where an optimistic update would buy
  ~200ms and risk showing a maintenance flag that did not take. Skip
  them. `useOptimistic` earns its keep on high-frequency user-authored
  writes; this is a read-mostly inventory whose writes are rare and
  consequential.
- **`use`.** Reads a promise or context during render and suspends. With
  TanStack Query already owning the fetch lifecycle, `use` adds nothing
  here; `useSuspenseQuery` is the Query-native path if Suspense is ever
  wanted.
- **Error boundaries.** Still class-based or via `react-error-boundary`,
  and still the only thing that stops a render throw from unmounting the
  tree. react.dev's `use` reference shows the canonical nesting: the
  Error Boundary wraps the Suspense boundary, with `resetKeys` so a
  retry re-runs the fetch.

**Where each belongs in this app.** Suspense is optional — the explicit
`isPending` branches this codebase already writes are clearer than a
fallback for a UI with this few loading states, and switching to
`useSuspenseQuery` would mean losing `placeholderData: keepPreviousData`
semantics on the paginated table (a suspended component has no previous
render to keep). Error boundaries are **not** optional and are missing
entirely (F3): with react-router 8 the right place is an `errorElement`
on the layout route in `src/router.tsx`, which catches render errors from
the whole `<Outlet />` subtree and keeps the nav on screen.

---

## 4. Loading, empty and error states as a design problem

Three principles, and how this codebase scores:

**Loading**: a skeleton that matches the real content's geometry beats a
spinner, because it does not reflow when data lands.
`SitesOverviewPage.tsx:251-258` does this properly — `SkeletonCard` is a
fixed `h-[184px]` with a comment saying exactly why. `InventoryPage.tsx:333-337`
does not: "Loading servers…" centred in a 12-unit-tall paragraph, then
the table replaces it and everything below jumps.

**Empty**: an empty state must distinguish *nothing matches* from
*nothing exists* from *we could not look*, and should offer the next
action. `InventoryTable.tsx:212` says "No servers match the current
filters." — correct category, no next action (no "clear filters", no
statement of which filters are on). `SitesOverviewPage`'s per-card
"empty" (`:241-243`) is good: a configured site with zero servers and a
site that does not exist are different facts and the page shows the
difference, which the E2E suite asserts (`e2e/sites.spec.ts:12-19`).

**Error**: show what failed, in the operator's terms, with a way to
retry. `InventoryPage.tsx:339-347` and `ServerDetailPage.tsx:41-49`
unwrap `ApiError.problem.detail` (RFC 9457) first and fall back
sensibly — that is genuinely good and better than most codebases.
`SitesOverviewPage.tsx:298-306` does **not**: it renders
`error.message`, the raw fetch string, and then keeps rendering two
section headings over empty grids (F15), so the page reads as "there are
no sites" underneath an error. No page offers a retry button anywhere.

---

## 5. Virtualisation for a 10,000-row table — the verdict

**No. Do not virtualise. The API's own design already answers this, and
adding TanStack Virtual here would be a net loss.**

The argument, in order of decisiveness:

**1. You cannot get 10,000 rows to the client anyway.** The backend caps
page size at 200 (`backend/app/config/settings.py:69`,
`max_page_size: int = 200`, enforced at
`backend/app/api/v1/servers.py:185-190`). Filling a virtualised 10k list
therefore means ≥50 round-trips, and because the cursor is opaque,
HMAC-signed and forward-only, they are strictly **sequential** — each
request's cursor comes out of the previous response. No parallel
prefetch, no random access, no "jump to row 8,000". A virtualised
scrollbar that implies random access over a dataset that only supports
forward iteration is a UI lying about its own data source.

**2. There is no rendering problem to solve.** Virtualisation reduces DOM
node count. The table currently renders `PAGE_SIZE = 50`
(`InventoryPage.tsx:27`) rows of 4 cells — 200 elements. Chromium does
not care. Virtualisation is the answer at ~1,000+ simultaneously
rendered rows; this is 50.

**3. Virtualising a `<table>` costs real correctness.** TanStack Virtual's
own table example documents the workaround, because table rows cannot be
absolutely positioned:

> Because `<table>` rows cannot be absolutely positioned, this example
> avoids paddingTop/paddingBottom on tbody. Instead, it sets each row's
> height explicitly and uses translateY to position rows virtually.
> — `examples/react/table/src/main.tsx`, TanStack Virtual

with `transform: translateY(${virtualRow.start - index * virtualRow.size}px)`
per row. Dynamic heights need `ref={virtualizer.measureElement}` plus
`data-index` on every row, and a `ResizeObserver` + `virtualizer.measure()`
on width change and on `document.fonts.ready` (both shown in the Virtual
docs' `pretext` and `chat` examples). Every one of those is a place for a
row to end up one pixel off, and this table's cells are
`whitespace-nowrap` (`InventoryTable.tsx:189`) so a long model string
changes column widths, which changes row heights, which invalidates the
measurements.

**4. Accessibility gets worse, and you have to rebuild it.** A native
`<table>` gives a screen reader the real row count and position for free.
The moment you render 20 of 10,000 rows, that is gone and you owe
`role="grid"`, `aria-rowcount` on the grid, `aria-rowindex` on every
rendered row, and a roving-`tabindex` keyboard model — because the
browser can no longer Tab to a row that is not in the DOM. That is a
week of work to get back to where a plain table starts.

**5. It solves the wrong operator problem.** Nobody with 10,000 servers
wants to scroll past 10,000 servers. They want the 12 that are critical.
That is filtering and sorting, both already server-side, and the facet
counts (`InventoryPage.tsx:117-126`) already tell them how many each
filter would match before they click. That is a better answer than any
scroll optimisation.

**What to do instead**, in order:

1. Fix the sticky header (F2) — it does not currently work, which is the
   real "long list" complaint virtualisation is usually reached for.
2. Raise `PAGE_SIZE` from 50 toward the 200 the API allows. A full screen
   of results costs one request either way.
3. Replace the `cursorHistory` stack with `useInfiniteQuery` + a "Load
   more" button (§2), accepting the unbounded-memory trade because
   nobody presses it 200 times.
4. Add a sort by `last_seen_at` (F20) so "what has gone quiet" is a sort,
   not a scroll.

**Where virtualisation would be right, later**: a single dense node's
`HardwareTab` with several hundred DIMM/drive rows, where the whole
dataset genuinely is in hand and the container is bounded. Not yet, and
not a dependency worth adding for it today.

---

## 6. TanStack Table 9 — the `/legacy` entry point

`InventoryTable.tsx:1-10` uses `@tanstack/react-table/legacy`
(`useLegacyTable`, `legacyCreateColumnHelper`, `LegacyColumnDef`) and the
comment explains why: v9 replaced the v8 hooks with a feature-composition
API and ships `/legacy` as the supported migration path. That is a
correct and lazy call — but it is *deferred* work, not free work, and it
should be written down as such rather than sitting only in a code
comment. The table has three columns, no client-side sorting
(`manualSorting: true`), no filtering, no grouping and no row selection.
Migrating it to the v9 native API is an afternoon; staying on `/legacy`
is fine until the entry point is removed.

Worth noting for the record: with `manualSorting: true`, three columns
and no row model features in use, TanStack Table is currently earning
approximately nothing here beyond `flexRender` and a column definition
array. That is not a reason to rip it out — it is a reason not to grow
the dependency in the direction of "we need Table for X" without
checking whether `.map()` covers X.

---

## 7. Forms, filters and URL-as-state

`InventoryPage` puts every filter, the sort and the cursor in
`useSearchParams` (`:46, 60-74`) and documents it as a project
requirement, not polish (`:33-37`). That is the right model and it works:
a site card links straight into a pre-filtered list
(`SitesOverviewPage.tsx:147`), and `e2e/sites.spec.ts:42-43` asserts the
round trip.

Three gaps:

- **Write-per-keystroke** (F10). `onChange` → `updateFilters` →
  `setSearchParams` runs a router navigation per character; the debounce
  (`:61`) only delays the *request*. The standard pattern is local state
  for the input, mirrored to the URL from the debounced value, so the URL
  is a settled address rather than a keystroke log. `replace: true`
  already stops the history spam, which is half the problem.
- **Tab state is not in the URL** (F5) — the one place the page's own
  stated rule is not followed. See §9.
- **No un-filter affordance.** With six filters and a checkbox there is
  no "clear all", and no summary of what is currently on. The URL is the
  state, so a reset is `setSearchParams(new URLSearchParams())` — one
  line.

The ADR-0008 Playwright constraint is unchanged and this note does not
touch it: **never `getByLabel` on a page with sibling `<select>`s.**
`InventoryPage` has five sibling `<select>`s under wrapping `<label>`s
(`:222-318`), so it is exactly the page the quirk was found on. The
current specs correctly use `getByPlaceholder` (`e2e/inventory.spec.ts:18`)
and role queries. Nothing proposed here adds a `getByLabel`.

---

## 8. Bundle size and code splitting (Vite 8.2.1)

**Vite 8 changed the API this repo would reach for.** From the v8
migration guide:

> Removed object form `build.rollupOptions.output.manualChunks` and
> deprecate function form one. […] Rolldown provides a more flexible
> `codeSplitting` option as an alternative for manual code splitting.

and `build.rollupOptions` is now itself "Alias to `rolldownOptions`
… @deprecated Use `rolldownOptions` instead". So any manual-chunking
advice written for Vite ≤7 is wrong here; the current lever is
`build.rolldownOptions.output.codeSplitting`. `cssCodeSplit` defaults
`true`, `chunkSizeWarningLimit` defaults 500 kB — which is why the
478.87 kB single chunk measured above squeaks under the warning and
nobody has noticed.

The right first move is not manual chunking at all, it is **route-level
lazy loading**, which Vite splits automatically at the dynamic import.
react-router 8 supports both forms:

```ts
{ path: "/rules", lazy: () => import("./features/rules/RulesPage").then(convert) }
```

and the newer per-property object form, which the react-router changelog
recommends specifically to avoid shipping code that is never used after
hydration:

> Separate route properties like HydrateFallback into independent
> imports inside object-based `route.lazy` to avoid downloading unused
> code after initial hydration.

`RulesPage` + `types/health.ts` + `types/classification.ts` is ~330 lines
that an operator hunting a server never loads. `StatusPage` is a debug
page nothing links to.

**How to measure**: `rollup-plugin-visualizer` still works under
rolldown's Rollup-compatible plugin interface and is the standard answer;
add it as a dev dependency behind a `--mode analyze` build so it never
ships. Do the measurement *before* choosing chunks — the honest
possibility is that the 479 kB is mostly React + Query + Table + Router
in one vendor chunk, in which case route splitting moves very little and
the real win is dropping a dependency (see §6).

---

## 9. `oxlint` + `tsgolint` vs `eslint` + `typescript-eslint`

Measured on this repo, 2026-09-06, not inferred.

**What oxlint's type-aware mode covers.** Per oxc.rs' own type-aware
page: "This functionality is provided by tsgolint and is integrated
directly into the Oxlint CLI and configuration system, **currently
supporting 59 out of 61 rules from typescript-eslint**." So the
type-aware *rule gap* versus typescript-eslint is now essentially
closed — two rules. That is a different situation from 2025 and the
common "oxlint can't do type-aware" claim is out of date.

**What this repo actually gets from it — two measured facts.**

1. Type-aware rules *are* running. A probe file with an unawaited
   promise, linted with the repo's own `.oxlintrc.json`, produced:
   `warning typescript(no-floating-promises): Promises must be awaited`.
2. **But `npx oxlint` exited `0` with that warning present.** `npm run
   lint` is bare `oxlint` (`package.json`), so CI's frontend lint step
   passes with floating promises in the tree. `--deny-warnings` is the fix.
3. And the rules that are not in the default set do not run.
   `typescript/no-unnecessary-condition` — the rule that catches a
   `!== undefined` guard on a value that can also be `null`, i.e. exactly
   the class of bug commit 1a896af was — produced nothing on a probe that
   trips it. Its docs say it "was introduced in version 1.48.0" and
   requires type-aware linting plus explicit configuration.

`.oxlintrc.json` sets `typeAware: true` and then names only two React
rules. There is no `categories` block, so the default category set
applies and every opt-in type-aware rule is off.

**What oxlint still cannot do that `eslint` + plugins can**, and this is
the real gap now:

- **Ecosystem plugin rules with no oxlint port.**
  `@tanstack/eslint-plugin-query`'s `exhaustive-deps` (which would have
  caught F1 — a query key that omits an input the function depends on, or
  includes one it does not), `eslint-plugin-jsx-a11y` (which would flag
  the click-handler-on-`<tr>` in F14), and
  `eslint-plugin-testing-library`. Oxlint has ports of *some* jsx-a11y
  rules; it does not have the Query plugin.
- **Custom project rules.** No local-rule authoring story comparable to
  ESLint's.

**Recommended `.oxlintrc.json` changes** (small, high value):

```jsonc
{
  "plugins": ["react", "typescript", "oxc"],
  "categories": { "correctness": "error", "suspicious": "warn" },
  "options": { "typeAware": true },
  "rules": {
    "react/rules-of-hooks": "error",
    "react/only-export-components": ["warn", { "allowConstantExport": true }],
    "typescript/no-floating-promises": "error",
    "typescript/no-misused-promises": "error",
    "typescript/no-unnecessary-condition": "error",
    "typescript/no-unnecessary-type-assertion": "error"
  }
}
```

plus `"lint": "oxlint --deny-warnings"` in `package.json`.

`no-unnecessary-condition` will produce noise on first run against
`exactOptionalPropertyTypes` code. That noise is the point: each hit is
either a redundant guard or a guard written against the wrong half of
`X | null | undefined`.

---

## 10. Tailwind 4 conventions

`src/index.css` is, on the whole, a model of how to do Tailwind 4. It
uses `@theme` for tokens that should generate utilities (`:18-35`) and
plain `:root` for tokens that should not (`:37-76`) — which is exactly
the distinction the docs draw:

> The `@theme` directive is used instead of `:root` because it explicitly
> instructs Tailwind to generate utility classes… Defining regular CSS
> variables with `:root` is still useful for variables that should not
> have corresponding utility classes.
> — *Theme variables*, tailwindcss.com

There is no `tailwind.config.js`, correctly; the plugin is wired in
`vite.config.ts:8`. The oklch reasoning in the header comment (`:8-12`)
is sound and the light/dark token pairs are complete.

**On `@apply`**: the docs list it as a directive for "inlining existing
utility classes into your custom CSS", with the main documented use being
third-party CSS you do not control (their example is styling `select2`
markup). That is the right reading: `@apply` is for markup you cannot put
a class on. When you *can* put a class on it — i.e. every component in
this repo — extracting a React component is strictly better, because it
carries the behaviour and the accessibility attributes along with the
classes, and Tailwind's own reuse guidance points at conditional class
application in JSX rather than at `@apply`. This repo uses `@apply` zero
times. Keep it that way.

**`@utility` vs a component vs a shared class string.** The repo has one
plain utility class, `.tabular` (`:120-122`), declared as raw CSS rather
than `@utility`. `@utility` is the v4 mechanism for a custom utility that
should work with variants (`hover:tabular`, `md:tabular`). Since nothing
ever needs a variant of `tabular-nums`, plain CSS is the lazier and
correct choice — noted so a future session does not "fix" it.

**Class soup**, which is the one real Tailwind problem here. The pattern
in use is a module-level constant, e.g. `FIELD_CLASS`
(`InventoryPage.tsx:23-24`, 200 characters). That is a reasonable middle
rung. But the same 200-character string for the two pagination buttons is
duplicated inline (`InventoryPage.tsx:366` and `:374`) rather than shared,
and the `SiteCard` link class (`SitesOverviewPage.tsx:197-201`) and the
`InventoryTable` row class (`InventoryTable.tsx:186`) each run past 300
characters inline. Where two elements are genuinely peers, hoist one
constant; where a third appears, that is the signal to make a component.

**The token discipline is only half-applied** (F6). `index.css:40-41`
says "no component reaches for a raw gray", and then `Badge.tsx:12-15`,
`HealthBadge.tsx:6-12` and `LinkStateBadge.tsx:3-15` all reach for raw
`bg-green-100 / dark:bg-green-900/40` palette values. Those three are the
older components; `StateBadge` and the two page shells use tokens. The
consequence is not cosmetic — see §12.

---

## 11. Accessibility, with teeth

**Keyboard operability.** The name cell is a real `<Link>`
(`InventoryTable.tsx:76-82`) with a documented rationale — correct, and
the reason ctrl-click works. But the `<tr>` around it carries an
`onClick`, `cursor-pointer` and `focus-visible:outline-*` classes
(`:172-186`) with **no `tabIndex` and no key handler** (F14), so the row
is mouse-only and its focus ring is unreachable dead styling that implies
an affordance that does not exist. Either drop the classes or make rows
genuinely focusable — do not leave the implication.

**Focus management on route change** (F13). Nothing in `AppLayout.tsx`
moves focus or announces a navigation. In an SPA, clicking a link leaves
focus on the (now unmounted) link's position and a screen reader says
nothing, so a keyboard user re-tabs through the nav on every page. The
minimum is a focusable `<h1>`/`<main>` that receives focus after
navigation, plus a skip link — neither exists.

**Nav state** (F12). `AppNav` (`:14-46`) computes `isActive` by hand and
expresses it as a colour plus an `aria-hidden` 2px bar, so **nothing
announces which page is current**. react-router ships `NavLink`, which
sets `aria-current="page"` for you. This is rung 4 of the ladder: use the
platform feature that already exists. (The detail page's tabs *do* set
`aria-current`, `ServerDetailPage.tsx:65` — so the codebase knows the
attribute; the nav just predates it.)

**ARIA for a data grid.** The current native `<table>` with `scope="col"`
headers is correct and needs no ARIA — which is the strongest argument in
§5. Two small gaps: the sort direction is a bare `▲`/`▼` span marked
`aria-hidden` (`InventoryTable.tsx:149-152`), so a screen reader hears
"Name, button" with no indication of sort state — `aria-sort` on the
`<th>` is the fix; and the table has no `<caption>` or `aria-label`.

**Contrast.** Well handled and deliberately so — `index.css:10-12` states
that every status colour clears WCAG AA on its paired surface in both
schemes, and holding L constant across hues is the right technique. This
was not re-measured for this note.

**What a screen reader makes of a status badge.** For `StateBadge`, the
correct answer: the glyph is `aria-hidden` (`:75`) and the word "Critical"
is real text, so it announces "Critical". For `HealthBadge`
(`:14-21`) it announces "CRITICAL" — all-caps, which some screen readers
spell out letter by letter — and carries no glyph at all. The
colourblind-safety design documented at length in `severity.ts:11-17` and
`StateBadge.tsx:18-22` therefore holds on the inventory table and
**nowhere else**: on the detail page and the Rules page, severity is
colour plus a shouted word. `LinkStateBadge` has the same shape ("UP",
"DOWN"), though its coloured dot is at least a second channel.

---

## 12. Assessment of the actual code

Ordered by value. Effort: XS < 30 min, S ≈ 1 h, M ≈ half a day.

### F1 — facets query key includes params the request strips (S)

`src/api/queryKeys.ts:17-18`, `src/api/servers.ts:53-58`,
`src/features/inventory/InventoryPage.tsx:107`.

`getServerFacets` strips `cursor`/`page_size`/`sort`/`sort_desc`/`with_count`
so the counts describe the whole filtered set. The key factory is handed
those same params unstripped. Every Next, Previous and sort click
therefore issues a byte-identical `/api/v1/servers/facets` request under a
fresh cache key. Walking a 10k-server list at 50/page is ~200 wasted
requests.

*Fix*: derive a `filterParams` memo in `InventoryPage` and pass it to both
`queryKeys.servers.facets` and `getServerFacets`; or strip inside the key
factory so the two can never diverge again. The second is lazier and
closes the hole permanently.

### F2 — the sticky table header does not stick (S)

`src/features/inventory/InventoryTable.tsx:126` (wrapper `overflow-hidden`)
and `:130` (`thead` `sticky top-0 z-10`).

`position: sticky` is resolved against the nearest ancestor **scroll
container**, and any ancestor with `overflow` other than `visible` becomes
one. The wrapper is `overflow-hidden`, so it is the sticky container — and
it never scrolls, so the header never moves relative to it and simply
scrolls off with the page. The comment at `:128-129` ("at 100+ rows the
column meaning otherwise scrolls away exactly when you are deep enough to
need it") describes a behaviour the code does not have.

*Fix*: make the wrapper a real scroll container (`max-h-[…] overflow-y-auto`)
so the header sticks inside it, or remove `overflow-hidden` (clip the
corners with `isolate` + rounded children) so it sticks to the document.
Confirm in a browser at 50 rows either way — this one is easy to "fix"
into a different non-working state.

### F3 — no error boundary anywhere (S)

`src/main.tsx:16-21`, `src/router.tsx:10-47`.

No `errorElement`, no `ErrorBoundary`, no class boundary. A render throw
anywhere unmounts the entire app to a blank white page with the message
only on the console. This is not hypothetical: it is precisely what
commit 1a896af did (a `null` reaching `.toFixed()`), and
`e2e/unread-fields.spec.ts` exists *because* the failure was invisible —
its comment at `:29-30` says "A React render that throws unmounts
silently as far as the DOM is concerned; the only durable evidence is on
the console."

*Fix*: `errorElement` on the layout route, rendering `useRouteError()`
with the nav still on screen and a reload action. Optionally a second one
per page route so a broken detail page does not take the list with it.

### F4 — "Back to inventory" goes to the sites overview (XS)

`src/features/servers/ServerDetailPage.tsx:35-37` links to `/`;
`src/router.tsx:18-20` maps `/` to `SitesOverviewPage`. The label is
wrong and the destination discards whatever filters the operator arrived
with.

*Fix*: `to="/servers"`, and better, carry the list's search string
(the router's location state, or just `-1` via `useNavigate`).

### F5 — detail-page tab is component state, not URL (S)

`src/features/servers/ServerDetailPage.tsx:27`.

`useState<TabId>("overview")`. An operator cannot send a colleague "the
Hardware tab of this box", a refresh silently resets to Overview, and the
back button skips the tab history. This is the exact rule
`InventoryPage.tsx:33-37` states for itself and is worth applying
consistently.

*Fix*: `useSearchParams` with `?tab=hardware`, validated the same way
`isSortableField` validates `sort`.

### F6 — two severity vocabularies in two colour systems (M)

`src/components/StateBadge.tsx:33-59` (tokens, glyph, "Critical") versus
`src/components/HealthBadge.tsx:6-21` (raw palette, no glyph, "CRITICAL").
`Badge.tsx:12-15` and `LinkStateBadge.tsx:3-15` also use the raw palette,
against `index.css:40-41`.

`HealthBadge` is what the detail page (`OverviewTab.tsx:44`), the hardware
tables (`HardwareTab.tsx:265`) and the Rules page (`RulesPage.tsx:195`)
render. So the colourblind-redundancy guarantee — three signals: shape,
word, colour — that `severity.ts:11-17` explains at length applies to one
screen only.

*Fix*: give `HealthBadge` the same body as `StateBadge`'s severity chip
(glyph + sentence-case label + tokens), or delete it and have callers use
`StateBadge` with a `maintenance`-less variant. Then move `Badge` and
`LinkStateBadge` onto tokens.

### F7 — no code splitting; one 479 kB chunk (S)

`src/router.tsx:3-8`. Measured 478.87 kB / 140.59 kB gzip, one file. See
§8 for the Vite 8 API caveat (`rolldownOptions` / `codeSplitting`,
`manualChunks` object form removed) and for measuring before chunking.

### F8 — dead code and stale shipped copy (S)

- `src/features/events/HistoryPanel.tsx` (123 lines) and
  `HistoryPanel.test.tsx` (90 lines): **zero importers**, verified by
  grep across `src`. Left behind when the rule/policy editor pages were
  removed.
- `src/api/healthMetrics.ts` and `queryKeys.healthMetrics`
  (`queryKeys.ts:33-36`): unreferenced. The docstring still describes
  "the health-policy condition builder's metric picker", a page that no
  longer exists.
- `src/api/events.ts`: `listEvents` fed only `HistoryPanel`;
  `listServerEvents` has no caller at all.
- `src/routes/StatusPage.tsx:19-21` ships "Phase 1 skeleton — inventory
  table lands in the next slice" on a live route, and `/status`
  (`router.tsx:42`) is linked from nowhere.

*Fix*: delete the first three. Keep `/status` if it is genuinely wanted as
a debug page — but then fix the copy and link it, because an unlinked
route with a lie on it is worse than no route.

### F9 — lint gate is softer than it looks (S)

`.oxlintrc.json`, `package.json`'s `"lint": "oxlint"`. Measured: a
floating promise produces a warning and `oxlint` exits `0`, so CI passes;
`no-unnecessary-condition` is off. See §9 for the recommended config.

### F10 — a router navigation per keystroke (S)

`src/features/inventory/InventoryPage.tsx:213-219` → `:132-149`. The
debounce at `:61` protects the network, not the render. Hold the input in
local state; write the debounced value to the URL.

### F11 — layout shift on every fetch (XS)

`src/features/inventory/InventoryPage.tsx:351-353`. The "Updating…"
paragraph is *inserted*, pushing the table down a line on every keystroke,
sort and page change. It is also `text-gray-400`, off-token.

*Fix*: reserve the row (or overlay it), token the colour, and add
`aria-live="polite"` so the state change is announced rather than only
seen.

### F12 — nav announces no current page (XS)

`src/components/AppNav.tsx:14-46`. Use `NavLink`; it sets
`aria-current="page"` and gives `isActive` to the class callback, which
also deletes the `useLocation` + hand-rolled prefix logic at `:21-22`.

### F13 — no focus management on navigation, no skip link (S)

`src/components/AppLayout.tsx:8-15`.

### F14 — unreachable focus ring on a mouse-only row (XS)

`src/features/inventory/InventoryTable.tsx:172-186`.

### F15 — error state leaves empty section headings (XS)

`src/features/sites/SitesOverviewPage.tsx:298-328`. Also uses
`error.message` rather than `ApiError.problem.detail`, unlike every other
page in the app.

### F16 — query defaults not tied to anything real (S)

`src/lib/query-client.ts:11-18`. See §2.

### F17 — site id interpolated into a URL unescaped (XS)

`src/features/sites/SitesOverviewPage.tsx:147`. Site codes come from
`INVENTORY_SITES` at runtime (ADR-0018), so the frontend cannot assume
they are URL-safe. `createSearchParams({ site_id: site.site_id })`.

### F18 — hook call hidden inside an argument (XS)

`src/features/inventory/InventoryPage.tsx:51`:
`const sites = siteOptions(useSitesQuery().data?.items);`. Legal and
unconditional, so no rules-of-hooks violation — but a hook call inside a
call expression is easy to move into a conditional later without noticing.

### F19 — non-issue, recorded so nobody "fixes" it

`src/features/sites/SitesOverviewPage.tsx:271-272` recomputes
`fleetCards`/`siteCards` (and the `sumBreakdowns` folds under them) on
every render with no `useMemo`. At ~6 sites × 4 vendors this is
microseconds. Leave it.

### F20 — cannot sort by `last_seen_at` (XS)

`src/features/inventory/InventoryPage.tsx:29-31` and
`src/features/inventory/InventoryTable.tsx:49` restrict sorting to
`name | model | updated_at`, while `ServerListParams["sort"]`
(`src/api/servers.ts:19`) and the backend both accept `serial` and
`last_seen_at`. "Sort by least recently seen" is the single most useful
sort for the staleness problem CLAUDE.md lists as not-done item 0, and it
is already implemented server-side.

---

## 13. Operator UX — what would actually make someone faster

This tool exists to answer two questions: *which box is this*, and *why
is it unhealthy*. Measured against that:

**1. The detail page does not answer "why is it unhealthy". Fix this first.**

`HealthSummary` carries seven severities — `overall`, `cpu`, `memory`,
`storage`, `network`, `connectivity`, `power` (`types/server.ts:57-65`) —
and the API sends all seven on every response. `OverviewTab.tsx:44`
renders **one** of them, `overall`. So an operator who opens a CRITICAL
server is told it is critical and is given no indication of which
category caused it, and must guess which of four tabs to open. Rendering
the six category severities as a row of chips beside "Overall health" is
maybe twenty lines and turns a hunt into a glance.

The step beyond it: nothing anywhere names *which policy* fired.
`RulesPage` lists every policy with its condition
(`RulesPage.tsx:197-201`) — the ingredients are present; the join is not.
Even linking "Storage: CRITICAL" to the Rules page anchored at the
storage policies would close most of the gap.

Classification already does this correctly, which is the proof it is
worth doing: `Classification.matched_rule_id` exists
(`types/server.ts:51`) — though it is not rendered either
(`OverviewTab.tsx:42` shows the type, not the rule).

**2. `unread_fields` reads honestly — on one tab out of four.**

The Hardware tab is genuinely well done. `Reported`
(`HardwareTab.tsx:219-253`) distinguishes three states properly: read
(render), not-read-and-empty ("Not reported", not a confident `0`), and
not-read-but-carried-forward (rendered, dimmed, with an explanation).
That is exactly the distinction the whole backend turns on and the UI
honours it. `e2e/unread-fields.spec.ts:48-50` asserts no `NaN`, `null` or
`undefined` reaches the page.

Two gaps:

- **Only Hardware gets it.** `ServerDetailPage.tsx:94-99` passes
  `unreadFields` to `HardwareTab` and to nothing else, but the field also
  carries `identity.nic_macs` and network paths. So a NIC list the
  collector could not read renders on the Network tab as a confident
  empty list — the precise failure `unread_fields` was invented to
  prevent, one tab over. `NetworkTab.tsx:72-76`'s comment even states the
  principle ("A missing row reads as 'does not apply'; a dash says the
  collector looked and got nothing") while the tab has no way to tell the
  two apart.
- **The explanation is `title=`-only** (`HardwareTab.tsx:238, 242, 249, 288`).
  A `title` attribute is mouse-hover only: it is invisible to keyboard
  users, unreliable on touch, and inconsistently announced by screen
  readers. The strings (`NOT_READ_TITLE`, `STALE_TITLE`) are good writing
  hidden behind a bad delivery mechanism. A visible `†`-style marker with
  one legend line per section would put the same information in front of
  everyone.

**3. Health severity is scannable in the table and nowhere else.**

The inventory table is the best-designed screen here: distinct glyph +
word + colour + a 2px left accent only on rows that need attention
(`InventoryTable.tsx:36-47`) — accenting nothing that is fine is the
right instinct and the reasoning is written down. Then the detail page
renders the same severity as a shouted, glyphless, differently-coloured
chip (F6). One badge, used everywhere, would make severity mean one thing
across the app.

**4. Filter discoverability is a real strength; the exits are missing.**

The facet counts (`InventoryPage.tsx:117-126`) are the best UX decision in
the codebase — an operator sees how many servers each option would match
*before* clicking, and the `withCount` docstring's refusal to show a
possibly-wrong number is the right kind of care. What is missing is the
way out: no "clear all filters", no visible chip-row of what is currently
applied, and an empty state (`InventoryTable.tsx:212`) that says "No
servers match the current filters" without naming them or offering to
drop one. An operator who lands on an empty table from a stale bookmarked
URL has no idea which of six filters is responsible.

**5. Keyboard.** Today: no skip link, no focus move on navigation
(F13), no `aria-current` in nav (F12), rows not focusable (F14), sort
state not announced (§11), and no `/`-to-focus-search. For a tool
someone is in all day while on a call, that last one alone is worth more
than most of the visual work.

**6. Error copy should say what to do.** Every error state in the app
describes the failure and stops. `ApiError.problem` carries `code` and
`request_id` (`api/client.ts:8-17`) and neither is ever shown — a
`request_id` in the corner of an error is the difference between "the
page is broken" and a support ticket someone can act on. And no error
state anywhere has a retry button, though `refetch()` is one destructure
away.

**7. Sort by "least recently seen"** (F20) — the query an operator asks
when something has gone quiet, already supported by the backend, not
offered by the UI.

---

## Sources

All via `context7`, against each project's own documentation:

- React — `react.dev` (`/websites/react_dev`): React 19 release post
  (ref-as-prop, Actions, `useActionState`), `use` reference (Error
  Boundary + Suspense nesting).
- TanStack Query — `/tanstack/query`: `useInfiniteQuery` reference
  (`initialPageParam`, `getNextPageParam`, `maxPages`), *Important
  Defaults*, *Caching* (`gcTime` 5 min / `staleTime` 0),
  `QueryClient.invalidateQueries` and the `queryClient.ts` source for
  prefix matching, v5 migration guide (`maxPages` requires
  bi-directional).
- TanStack Virtual — `/tanstack/virtual`: `docs/introduction.md`,
  `examples/react/table/src/main.tsx` (the `translateY` table workaround),
  `docs/chat.md` (`measureElement`), `docs/pretext.md` (re-measure on
  resize and `document.fonts.ready`), sticky-`thead` example.
- Tailwind CSS 4 — `/websites/tailwindcss`: *Theme variables* (`@theme`
  vs `:root`, referencing other variables), *Functions and directives*
  (`@utility`, `@apply`), *Compatibility* (`@reference`), *Styling with
  utility classes*.
- React Router — `/websites/reactrouter`: `createBrowserRouter` API,
  `lazy` route object property, object-form `lazy` per-property splitting,
  `Component`/`ErrorBoundary` module exports.
- Vite 8 — `/vitejs/vite/v8.0.10`: `docs/guide/migration.md` (object-form
  `manualChunks` removed, function form deprecated, `codeSplitting`
  replacement), `docs/guide/build.md` (chunking strategy),
  `packages/vite/src/node/build.ts` (`rollupOptions` deprecated alias for
  `rolldownOptions`, `cssCodeSplit`, `chunkSizeWarningLimit` 500 kB).
- oxlint / tsgolint — `/websites/oxc_rs_guide_usage`: type-aware guide
  ("59 out of 61 rules from typescript-eslint"), `typeAware` option,
  `typescript/no-unnecessary-condition` rule page (requires type-aware,
  since 1.48.0).
- This repository: `frontend/package-lock.json`,
  `backend/app/config/settings.py:69`, `backend/app/api/v1/servers.py:185-190`,
  and the files cited inline. Build and lint measurements run locally
  2026-09-06.
