# Frontend audit — `frontend/src`, `frontend/e2e`, frontend config

Read-only audit for the production-hardening pass. **No source was
changed.** Branch `dev-refactor`, audited at commit `f9ab059`.

`npm run lint` (oxlint 1.78.0, type-aware) and `npm run typecheck`
(`tsc -b`) both pass with zero output. Everything below is a judgement
about the code, not a tool finding.

---

## 1. Map

### 1.1 Routes

All routes are children of `AppLayout` (`src/router.tsx:10`), which is
`<AppNav/>` + `<Outlet/>` and nothing else.

| Path | Component | Purpose |
|---|---|---|
| `/` | `SitesOverviewPage` | Landing page. 3 fleet cards + 1 card per site. |
| `/servers` | `InventoryPage` | The inventory table + filter bar + cursor pagination. |
| `/servers/:id` | `ServerDetailPage` | 4 tabs: Overview, Hardware, Network, Connectivity. |
| `/rules` | `RulesPage` | Read-only classification rules + health policies. |
| `/status` | `StatusPage` | Backend-readiness debug page. |

There is no `errorElement` on any route and no error boundary anywhere in
the tree, so a render throw blanks the whole app (this is exactly the
failure mode `e2e/unread-fields.spec.ts` was written to catch after
commit `1a896af`).

### 1.2 Component tree

```
main.tsx  → QueryClientProvider → RouterProvider
  AppLayout
    AppNav                                  (3 links, active = pathname prefix)
    SitesOverviewPage
      SectionHeading, SkeletonCard
      SiteCard → VendorBar                  (card = <Link> to a pre-filtered list)
    InventoryPage
      <form> of 6 inline filter controls    (all markup inline in the page)
      InventoryTable → StateBadge           (TanStack Table v9 /legacy)
      Previous / Next buttons
    ServerDetailPage
      tab <button>s (state in useState)
      OverviewTab   → Field, OpenShiftValue, Badge, HealthBadge
      HardwareTab   → Reported, Stat, Health → HealthBadge
      NetworkTab    → Stat, LinkStateBadge
      ConnectivityTab → FabricCard → DetailRow, LinkStateBadge
    RulesPage       (two tables, all markup inline)
    StatusPage
```

Shared components: `Badge`, `HealthBadge`, `StateBadge`, `LinkStateBadge`,
`severity.ts` (`SEVERITY_GLYPH`, `isHealthSeverity`).

**Orphan:** `src/features/events/HistoryPanel.tsx` (123 LOC) has no
production caller — see finding F-07.

### 1.3 Where state lives

- **Server state:** TanStack Query, one shared client
  (`src/lib/query-client.ts:11`): `staleTime: 30_000`, `retry: 1`,
  `refetchOnWindowFocus: false`. No `gcTime` is set anywhere, so every
  query uses the 5-minute default.
- **Inventory filters + sort + cursor:** the URL
  (`useSearchParams`, `InventoryPage.tsx:46`). Shareable and
  refresh-safe. This is the right call and it is done well.
- **`cursorHistory`** (the back-stack for "Previous"): component
  `useState` (`InventoryPage.tsx:58`). Documented as a deliberate
  limitation — does not survive a reload.
- **Active detail tab:** component `useState`
  (`ServerDetailPage.tsx:27`) — *not* in the URL. See F-05.
- **Maintenance reason draft:** `useState` in `OverviewTab.tsx:26`.

No context, no Redux, no Zustand. No prop drilling worth naming: the
deepest prop chain is `ServerDetailPage → HardwareTab → Reported`, two
levels.

### 1.4 Every query and mutation

| Hook | Key | staleTime | gcTime | Placeholder | Invalidation |
|---|---|---|---|---|---|
| `useServersQuery` (`features/inventory/hooks.ts:14`) | `["servers","list",params]` | 30s (default) | default | `keepPreviousData` | none |
| `useServerFacetsQuery` (`hooks.ts:29`) | `["servers","facets",params]` | 30s | default | `keepPreviousData` | none |
| `useServerDetailQuery` (`features/servers/hooks.ts:7`) | `["servers","detail",id]` | 30s | default | — | — |
| `useSitesQuery` (`features/sites/hooks.ts:14`) | `["sites","list"]` | 30s | default | — | none |
| `useClassificationRulesQuery` | `["classificationRules","list",params]` | 30s | default | — | none |
| `useHealthPoliciesQuery` | `["healthPolicies","list",params]` | 30s | default | — | none |
| `StatusPage` inline `useQuery` (`routes/StatusPage.tsx:11`) | `["platform","readiness"]` — **bypasses `queryKeys`** | 30s | default | — | — |
| `HistoryPanel` `useQueries` (dead code) | `["events","list",params]` | **0** | default | — | — |
| `useEnableMaintenanceMutation` | — | — | — | — | `setQueryData` on the detail key only |
| `useDisableMaintenanceMutation` | — | — | — | — | `setQueryData` on the detail key only |

`queryClient.invalidateQueries` is **never called anywhere in the app.**
The two maintenance mutations write the mutation's own response into the
detail cache and stop there — the servers *list* and the *sites* overview
both keep a stale maintenance count for up to their 30s `staleTime` plus
however long until a remount. See F-04.

### 1.5 API client layer

`src/api/client.ts` is a 63-line `fetch` wrapper. It is thin and correct
in shape:

- Merges headers through the `Headers` API rather than object-spreading
  `HeadersInit` (a real trap, correctly avoided — `client.ts:39`).
- Parses a non-2xx body as RFC 9457 problem details and throws a typed
  `ApiError` carrying `problem` (`client.ts:19`, `:50-56`), falling back
  to a plain `Error` when the body is not JSON.
- Returns `undefined as T` for 204.

**Response typing is hand-written, not generated** (`src/types/server.ts:1`
states the choice and its reasoning). There is no runtime validation at
the boundary — `client.ts:62` is `(await response.json()) as T`. The
module docstring at `types/server.ts:8-18` is genuinely excellent (the
`X | null` vs `X | undefined` rule, with the commit that caused it), but
the approach has already drifted once and nothing caught it:
`types/server.ts:138` records that `MemoryModule.speed_mts` "matched no
field the API has ever sent." See F-08.

**Keyset cursor:** handled in `InventoryPage`, not in the client. The
client just passes `cursor` through (`api/servers.ts:21`). Forward paging
uses `data.page.next_cursor`; backward paging uses the local
`cursorHistory` stack. `updateFilters` deletes `cursor` on every filter
change (`InventoryPage.tsx:143`), which is correct — the backend rejects
a cursor from before a filter change.

---

## 2. Findings, ranked

Severity categories: **correctness** / **maintainability** /
**performance** / **consistency** / **accessibility**.

### High

| # | Location | Finding | Category | Fix | Effort |
|---|---|---|---|---|---|
| **F-01** | `src/features/sites/SitesOverviewPage.tsx:236` | A site whose servers are all `UNKNOWN` renders **"● all healthy"**. The condition is `critical === 0 && warning === 0 && total > 0` — `UNKNOWN` and `INFO` are both ignored. A site the health engine has never evaluated, or one whose collector last failed, makes a confident green claim. This is the same class of error as the confident zero that `unread_fields` exists to prevent, in the one place an operator looks first. | correctness | Add `unknown > 0` and `info > 0` branches; only say "all healthy" when `by_health.HEALTHY === total`. Show `○ N unknown` in the muted colour otherwise. | S |
| **F-02** | `src/features/servers/ServerDetailPage.tsx:88-90` + `OverviewTab.tsx:52-84` | **A failed maintenance write is completely silent.** Only `isPending` is read off the two mutations; `isError`/`error` are never consulted, and there is no `onError`. The button re-enables and the badge does not change — indistinguishable from a slow success. This is the only write path in the entire application. | correctness | Pass `maintenanceError` down and render the `ApiError.problem.detail` next to the control, with `role="alert"`. | S |
| **F-03** | `src/features/inventory/InventoryPage.tsx:198-201` | The result-count header is **dead code**. It renders `data.page.count` when it is a number, but the backend gates `count` behind `with_count` (`backend/app/api/v1/servers.py:183`, default `False`) and `InventoryPage` never sends it (`with_count` is declared at `api/servers.ts:23` and set by nobody). The header therefore *always* falls through to "Browse and filter discovered servers." The only place an operator can see a total is inside a `<select>`'s "All (N)" option label. | correctness | Either send `with_count: true` on the first page, or delete the dead branch and render `facets.total` — which the page already has. | S |
| **F-04** | `src/features/servers/hooks.ts:19-21`, `:29-31` | Toggling maintenance updates only the detail cache. The inventory list (`["servers","list",…]`), its facets, and the sites overview (`in_maintenance` counts on every card) all keep the pre-toggle value. `invalidateQueries` is never called anywhere in the app. `e2e/maintenance.spec.ts:34` hides this by doing a full `page.goto("/servers")`. | correctness | Add `queryClient.invalidateQueries({ queryKey: queryKeys.servers.lists() })` and `{ queryKey: queryKeys.sites.all }` to both `onSuccess`. | S |
| **F-05** | `src/features/servers/ServerDetailPage.tsx:27,35,57-75` | Three problems in one block. (a) The active tab is `useState`, so **`/servers/x` on the Hardware tab is not linkable and Back does not undo a tab switch** — the browser Back button leaves the detail page entirely. (b) The "← Back to inventory" link at `:35` goes to `"/"`, which is the **Sites** page, not the inventory — and it discards the filters the operator arrived with. (c) The tabs are `<button>`s inside `<nav aria-label="Server detail tabs">` carrying `aria-current="page"`; they are keyboard-operable but the landmark and the attribute both claim navigation that does not happen. | correctness / accessibility | Put the tab in the URL (`?tab=hardware`) and make the four tabs `<Link>`s inside the nav — this fixes all three at once. Make the back link `useNavigate(-1)` or carry the inventory search string. | M |
| **F-06** | `src/features/inventory/InventoryPage.tsx:222-318` | **Every filter `<select>` has a corrupted accessible name.** Each is wrapped in a `<label>` whose text content is the label word *plus every `<option>`'s text* — the exact Chromium behaviour ADR-0008 documents. A screen reader announces the Vendor control as "Vendor dell (412) cisco (203) hp (91) standalone (14), combo box". ADR-0008 recorded this as a *test-selector* problem on pages that have since been deleted; it is live today on the inventory filter bar as a real accessibility defect, on five controls. | accessibility | Give each `<select>` an `id` and use a sibling `<label htmlFor>` instead of wrapping. (Bonus: `getByLabel` becomes usable again in E2E.) | S |
| **F-07** | `src/features/events/HistoryPanel.tsx`, `HistoryPanel.test.tsx`, `src/api/events.ts`, `src/types/events.ts`, `src/api/healthMetrics.ts`, `queryKeys.events`, `queryKeys.healthMetrics`, plus ~14 exports in `types/health.ts` and `types/classification.ts` | **~450 LOC of dead code** left behind when the rule/policy editors were removed. Verified with a whole-tree grep: no production importer. Dead: `HistoryPanel` + its test; `listEvents`, `listServerEvents`, all of `types/events.ts`; `listHealthMetrics`, `HealthMetricResponse`, `HealthMetricListResponse`; `OPERATOR_ALLOWED_TYPES`, `ALL_OPERATORS`, `operatorsForMetricType`, `EXISTENCE_OPERATORS`, `COUNT_OPERATORS`, `SET_OPERATORS`, `LIST_ELEMENT_OPERATORS`, `emptyPolicyScope`, `POLICY_CATEGORIES`, `PolicyCategory`, `HealthPolicyPreviewSample`; `RULE_SOURCES`, `RULE_SOURCES_FOR_CREATE`, `PRIORITY_BANDS`, `CLASSIFIABLE_FIELDS`, `ClassifiableField`, `emptyRuleScope`, `defaultRuleFlags`, `ClassificationPreviewSample`. Also `public/icons.svg` (referenced by nothing, still shipped in the build). | maintainability | Delete. **But see the operator-UX section — `listServerEvents` and `HistoryPanel` should be *wired up*, not deleted;** they are most of the "when did this change" feature that is currently missing. | M |

### Medium

| # | Location | Finding | Category | Fix | Effort |
|---|---|---|---|---|---|
| **F-08** | `src/api/client.ts:51`, `:62` | Two unchecked casts at the trust boundary. `:51` casts *any* JSON error body to `ProblemDetails` — a JSON error from an ingress or a proxy (not the app's own handler) produces an `ApiError` whose `problem.detail` is `undefined`, and every error renderer in the app prints `error.problem.detail` straight to the operator, so the screen reads "undefined". `:62` casts the success body with no validation, which is what let `speed_mts` (`types/server.ts:138`) survive undetected. | correctness | `:51` — guard on `typeof problem?.detail === "string"` before constructing `ApiError`, else fall through to the generic `Error`. `:62` — a full schema-validation layer is out of proportion; instead add a backend-side contract test in the shape of `tests/unit/test_frontend_manager_types.py`, which already proves this pattern works for `SOURCE_PROVIDERS`. | S / M |
| **F-09** | `src/api/queryKeys.ts:17-18` ← `InventoryPage.tsx:107` | The facets **cache key** includes `cursor`, `sort`, `sort_desc` and `page_size`, even though `getServerFacets` deliberately strips them from the *request* (`api/servers.ts:53-58`, with a comment saying exactly why). So the same facets response is cached under a different key on every page turn and every sort flip: one extra HTTP round trip per "Next" click, forever. Backend-side it hits Redis (`facets_key`), so the cost is the request, not the aggregation. | performance | Compute a `facetParams` object once in `InventoryPage` and pass it to both the hook and the key. Two lines. | S |
| **F-10** | `src/features/inventory/InventoryTable.tsx:134-158` | Sortable column headers carry no `aria-sort`. The sort direction is a `▲`/`▼` in an `aria-hidden` span, so a screen-reader user cannot tell which column is sorted or in which direction. | accessibility | `aria-sort={sorted === "asc" ? "ascending" : sorted === "desc" ? "descending" : "none"}` on the `<th>`. | S |
| **F-11** | `src/features/inventory/InventoryPage.tsx:351-353` | The "Updating…" indicator during a filter/page change is a plain `<p>` with no live region, and the row count change is never announced. A keyboard/screen-reader operator changing a filter gets no feedback that anything happened. Same file: the error `<p>` at `:340` has no `role="alert"` (only `SitesOverviewPage.tsx:300` uses one). | accessibility | `aria-live="polite"` on a status region that carries both the fetching state and the resulting row count; `role="alert"` on the three error paragraphs. | S |
| **F-12** | `src/features/servers/HardwareTab.tsx:238,242,249,288` | The whole "not read this run" / "carried forward" distinction is conveyed by **`title=` tooltips and `opacity-50`**. `title` is mouse-hover-only — invisible to keyboard and to most screen readers — and `opacity: .5` on text already at `--text-secondary` almost certainly drops below 4.5:1 on `--surface-raised`. The one mechanism in this codebase specifically designed to stop a confident zero is delivered through the two least accessible channels available. | accessibility | Replace `title` with a visible inline marker plus `<abbr>`/`aria-describedby`, or a small "unconfirmed" chip. Measure the dimmed contrast and raise it. | M |
| **F-13** | `src/components/Badge.tsx:13-15`, `HealthBadge.tsx:6-12`, `LinkStateBadge.tsx:3-15` | **Two colour systems coexist.** `StateBadge` and `severity.ts` use the `@theme`/`--tint-*`/`--text-on-*` tokens defined and carefully justified in `index.css:18-105`; `Badge`, `HealthBadge` and `LinkStateBadge` use raw Tailwind palette classes (`bg-green-100 dark:bg-green-900/40`). So `HEALTHY` is one green in the inventory table and a different green on the detail page, and the "L held ~0.62 so no severity looks louder than its rank warrants" reasoning in `index.css:25` is only true for half the app. | consistency | Rewrite the three badges onto the tokens. Mechanical. | S |
| **F-14** | `ServerDetailPage.tsx:35,39,42,54,56,68-69`; `RulesPage.tsx:82,100,106-121,…`; `HardwareTab`, `NetworkTab`, `ConnectivityTab`, `StatusPage` throughout | Same split at the surface level: `SitesOverviewPage`, `InventoryPage`, `InventoryTable`, `AppNav` and `StateBadge` use `var(--surface-*)`/`var(--text-*)`/`var(--border-*)`; the entire server-detail subtree, `RulesPage` and `StatusPage` use `text-gray-500 / border-gray-200 dark:border-gray-700 / text-blue-600`. Roughly: pages touched in the recent UI rebuild are tokenised, pages that predate it are not. | consistency | Convert the untouched files. Purely mechanical, but ~6 files. | M |
| **F-15** | `src/features/inventory/InventoryPage.tsx` (384 LOC) | The page holds URL-state parsing, the query-param builder, the cursor stack, the facet-label formatter (`withCount`, `:117`), six hand-rolled filter controls with an identical 200-character class string repeated seven times, the loading/error/empty branching, and the pagination controls. It is the only file in the tree that does everything at once. | maintainability | Extract a `<FilterBar>` (the `<form>`, `withCount`, `FIELD_CLASS`) and a `useInventoryFilters()` hook (the searchParams read/write, `queryParams`, `cursorHistory`). Leaves the page as composition. | M |
| **F-16** | `src/features/inventory/InventoryPage.tsx:29-31` vs `InventoryTable.tsx:49` vs `api/servers.ts:19` | `SortableField` is `"name" \| "model" \| "updated_at"` in the table, the `isSortableField` guard restates the same three literals by hand, and `ServerListParams["sort"]` allows five (`serial` and `last_seen_at` are reachable in the API and unreachable in the UI). Three copies of one list, in three files, already inconsistent. | maintainability | Derive the guard from a single `const SORTABLE_FIELDS = [...] as const`. | S |
| **F-17** | `.oxlintrc.json:3` | The lint config enables `react`, `typescript` and `oxc`. **`jsx-a11y` is not enabled**, so none of F-06, F-10, F-11 could have been caught mechanically. `react`'s `exhaustive-deps` is also not enabled. | maintainability | Add `"jsx-a11y"` to `plugins` and fix the fallout — likely small, since the code is generally careful. | S |

### Low

| # | Location | Finding | Category | Fix | Effort |
|---|---|---|---|---|---|
| **F-18** | `src/routes/StatusPage.tsx:5-8,20` | Live route `/status` still says "Phase 1 skeleton — inventory table lands in the next slice", ~7 slices later. Its query also bypasses the `queryKeys` factory with an inline `["platform","readiness"]`. | consistency | Reword; add `queryKeys.platform`. | S |
| **F-19** | `playwright.config.ts:25` | Comment points at `e2e/README.md`, which does not exist. | maintainability | Write it or drop the reference. | S |
| **F-20** | `src/features/servers/NetworkTab.tsx:65` | `key={`${iface.name}-${iface.mac}`}` — `mac` is nullable, so two same-named interfaces with unread MACs collide. `iface.name` is unique per server in practice, making the MAC redundant rather than helpful. | correctness | Use `${iface.name}-${index}`. | S |
| **F-21** | `src/features/servers/OverviewTab.tsx:37,39` | `server.identity?.vendor` / `server.identity?.serial` — optional chaining on a field the type declares non-optional (`ServerDetail.identity: ServerIdentity`). Either the type is wrong or the guard is noise; the same file dereferences `server.classification.installation_type` and `server.health.overall` unguarded. | consistency | Drop the `?.`, or fix the type if the API really can omit it. | S |
| **F-22** | `src/features/inventory/InventoryTable.tsx:186` | The `<tr>` carries `focus-visible:outline-*` classes but has no `tabIndex`, so it can never receive focus — dead CSS. (Keyboard operation is fine regardless: the name `<Link>` in each row is the real tab stop.) | maintainability | Delete the classes. | S |
| **F-23** | `src/api/servers.ts:53-58` | Five `void x;` statements to satisfy `noUnusedLocals` after a destructuring omit. Works, but `Object.fromEntries(Object.entries(params).filter(...))` or an explicit pick list reads better. | maintainability | Optional. | S |
| **F-24** | `frontend/nginx.conf` | No security headers on the SPA response: no `Content-Security-Policy`, `X-Content-Type-Options: nosniff`, `Referrer-Policy` or `X-Frame-Options`. `index.html` also gets no explicit `Cache-Control: no-cache`, so a browser may heuristically cache the one file that must not be cached (hashed assets correctly get `immutable`). | correctness | Add the four headers and a `location = /index.html { add_header Cache-Control "no-cache"; }`. Coordinate with the deploy auditor — the Route/ingress may already set some. | S |
| **F-25** | `package.json:11` | `"test": "vitest"` is watch mode. Every CI/agent invocation must remember `-- --run` (CLAUDE.md's verification block does). | maintainability | `"test": "vitest run"`, `"test:watch": "vitest"`. | S |

### Not findings — checked and clean

- **No `any`** in application code. The one `any` is
  `LegacyColumnDef<ServerSummary, any>` (`InventoryTable.tsx:65`), which
  is TanStack's own documented recommendation for heterogeneous `TValue`
  and is explained in a comment.
- **No non-null assertions (`!`) anywhere.** Zero. Under
  `noUncheckedIndexedAccess` + `exactOptionalPropertyTypes`, which is a
  genuinely strict pair.
- **No `@ts-ignore` / `@ts-expect-error` anywhere.**
- **No unused npm dependencies.** All seven runtime deps are imported:
  `@tanstack/react-table` via the `/legacy` entry point in
  `InventoryTable.tsx:9`.
- **No gratuitous memoisation.** The single `useMemo`
  (`InventoryPage.tsx:76`) is load-bearing — it stabilises the object
  that becomes a query key. No `React.memo`, no `useCallback`, and none
  is needed at these sizes.
- **No waterfall requests.** `InventoryPage` fires list + facets +
  sites concurrently on mount; `ServerDetailPage` fires one query.

---

## 3. Inventory table at 10k rows

**The table never renders more than 50 rows, so virtualisation is not
needed and is correctly absent.**

`PAGE_SIZE = 50` (`InventoryPage.tsx:27`) is sent on every request;
paging is keyset (`page.next_cursor`), never `skip`. `InventoryTable`
receives `data.items` and maps it — 50 `<tr>` × 4 `<td>`, three of which
are a single text node or one badge. At 10,000 or 50,000 servers the DOM
size and the render cost are identical to what they are at 51.

What actually scales:

- `keepPreviousData` on both queries (`features/inventory/hooks.ts:18,33`)
  keeps rows on screen through a page turn instead of flashing a spinner —
  the right choice for cursor pagination.
- Sorting is `manualSorting: true` (`InventoryTable.tsx:112`), so the
  server sorts; the client never sorts 10k rows.
- The sticky `<thead>` (`:130`) and the 2px severity row accent (`:41-47`)
  are pure CSS.

Two real costs at scale, neither about row count:

1. **F-09** — one avoidable facets request per page turn, because the
   cache key carries the cursor.
2. `page_size=50` with `has_more` but no total means an operator paging
   toward the end of a 10k list has no idea how far they are. See UX-3.

The only genuinely unbounded render in the tree is `HistoryPanel`, which
fetches `page_size: 200` per event type and merges client-side — and it
is dead code (F-07).

---

## 4. `unread_fields` — verdict

**Correctly implemented where it is rendered, and it is one of the best
things in this codebase. It does not re-introduce a confident zero.**

The backend emits exactly nine dotted paths
(`backend/app/application/services/ingest.py:489,562-629`):
`identity.nic_macs`, `hardware.cpu.{sockets,cores,threads,model}`,
`hardware.memory.total_bytes`, `hardware.storage.{total_bytes,drives}`,
`hardware.gpus`, `hardware.power.psus`.

`HardwareTab` consumes eight of the nine and gets the distinction exactly
right (`HardwareTab.tsx:219-253`, `:268-292`):

- unread **and** the stored value is the model's zero → the words
  **"Not reported"**, never `0` or an empty table;
- unread **but** a value was carried forward → the value is **shown,
  dimmed**, never hidden — with the tooltip "Not confirmed by the most
  recent collection";
- read → rendered untouched.

That is precisely the semantic CLAUDE.md asks for, and `HardwareTab.test.tsx`
asserts all three branches by name.

Three gaps, in descending order:

1. **F-12 — the distinction is delivered via `title=` and `opacity-50`.**
   Mouse-only and probably sub-contrast. The mechanism that exists to
   stop a false claim of fact is itself invisible to a keyboard user.
2. **Nothing above the Hardware tab says the server was only partly
   read.** Not on the detail header, not on the inventory row. An
   operator must open one specific tab to discover that this run failed
   to read anything at all. See UX-4.
3. `identity.nic_macs` is the one emitted path consumed by nothing — but
   `identity.nic_macs` is also not *rendered* anywhere (the Network tab
   reads `network.interfaces[].mac` instead), so no confident zero
   results from it today. It becomes a live bug the moment anyone
   displays that field.

Note also that `network.interfaces` and everything under `connectivity`
are **not tracked by the backend at all**, so `NetworkTab`'s "No network
interfaces." and `ConnectivityTab`'s "0/0 fabric paths up" are confident
zeros that the frontend currently has no way to qualify. That is a
backend gap, not a frontend one — flagging it for whoever owns ingest.

---

## 5. Operator UX

Everything in this section is **a proposal for the user to approve, not a
decision.**

### 5.1 "Which servers are unhealthy, and why?" — click count

**Which:** 3 clicks to a filtered list.

1. Land on `/` — the site cards already show `◆ 3 critical` / `▲ 7
   warning` per site. Excellent; the question "is anything wrong" is
   answered with zero clicks.
2. Click the card → `/servers?site_id=tlv`. **The card's link carries
   only the site, never the severity** (`SitesOverviewPage.tsx:147`), so
   you land on all 412 Tel Aviv servers, not the 3 critical ones.
3. Open the Health `<select>`, pick `CRITICAL` (2 interactions).

**Why:** not answerable at all, at any number of clicks.

4. Click a server → Overview shows exactly one health field:
   `Overall health: CRITICAL` (`OverviewTab.tsx:44`).

`HealthSummary` carries **seven** severities — `overall`, `cpu`,
`memory`, `storage`, `network`, `connectivity`, `power` — and the type is
declared in full at `types/server.ts:57-65`. A whole-tree grep finds
`health.overall` in three places and **every other category in zero.**
The six per-category severities are fetched on every detail request and
thrown away. The backend also sends `Health.evaluated_at`
(`backend/app/domain/models/health.py:31`), which the frontend type does
not even declare, so the UI can never say when health was last computed.

The backend records `HEALTH_STATUS_CHANGED` audit events with `{from,
to}` (`backend/app/application/services/ingest.py:452-458`) and exposes
`GET /api/v1/servers/{id}/events`. The client function for it —
`listServerEvents` (`api/events.ts:35`) — **is written and called by
nothing.**

> **UX-1 (proposal, effort S, highest value/effort ratio in this
> document).** Render the six categories on the Overview tab as a row of
> `StateBadge`s. The data is already on the wire and already typed. This
> takes "why" from *unanswerable* to *answered to the category level* in
> one small component.

> **UX-2 (proposal, effort S).** Make the `◆ 3 critical` and `▲ 7
> warning` counts on each site card their own links to
> `/servers?site_id=tlv&health_overall=CRITICAL`. Turns the 3-click
> journey into 1 click, using filter params the inventory page already
> parses.

> **UX-3 (proposal, effort M).** Add a **History** tab to
> `ServerDetailPage` backed by `listServerEvents` — `HEALTH_STATUS_CHANGED`
> ("CRITICAL → HEALTHY, 4 hours ago"), `MAINTENANCE_*`,
> `CLASSIFICATION_CHANGED`. This is the "why did this change and when"
> answer. Most of it already exists: `HistoryPanel` (F-07) is the right
> component with the wrong data source, and deleting it as dead code
> would throw away work that is nearly the feature.

### 5.2 Is severity scannable at a glance?

**Yes, and this is done unusually well.** `StateBadge`
(`components/StateBadge.tsx`) gives every severity a **distinct glyph**
(`◆ ▲ ■ ● ○`, `severity.ts:23`), **its own word**, and a colour — three
redundant signals, so colour is never load-bearing. `severity.ts:11-16`
records that a previous version gave `HEALTHY` and `INFO` the same glyph
and that `StateBadge.test.tsx` now asserts distinctness. `InventoryTable`
adds a 2px left row accent for `CRITICAL`/`WARNING` only
(`InventoryTable.tsx:41-47`), so a full page of rows has a scannable
vertical edge rather than fifty competing colours. Maintenance is shown
*alongside* severity in a hue outside the severity set, with the
reasoning written out at `StateBadge.tsx:6-22`.

Two caveats: the *detail* page uses `HealthBadge` instead, which has **no
glyph at all and a different palette** (F-13) — so the one screen where
you go to understand a problem is the one screen where severity is
colour-only. And the site cards deliberately show only critical/warning,
which is what produces F-01.

> **UX-4 (proposal, effort S).** Use `StateBadge` on the detail page too,
> and add an "unread fields" marker to the detail header — e.g.
> `⚠ 3 fields not read this run` linking to the Hardware tab. Closes both
> the glyph gap and gap 2 of section 4.

### 5.3 Filters: discoverable? shareable?

**Discoverable: yes, and better than most.** Six always-visible controls
in one row (`InventoryPage.tsx:203-330`) — no "Advanced filters"
disclosure to find. Every option is annotated with how many servers it
would match under the filters already applied
(`facets`, `withCount` at `:117`), and the `withCount` logic is
scrupulous: it stays silent rather than showing a wrong number when the
dimension is already filtered, and an option matching nothing renders
plain rather than "(0)" — `:109-126`.

**Shareable: yes.** Every filter, the sort and the cursor live in the URL
(`useSearchParams`), and site cards link to pre-filtered lists. This was
clearly treated as a requirement rather than polish, and it holds.

Gaps: there is no **"Clear all filters"** control (with six filters and
`replace: true` history writes, backing out of a filter set is tedious);
no visible chip summary of what is currently applied; and the sort field
is in the URL but there is no UI affordance beyond clicking the two
sortable headers.

> **UX-5 (proposal, effort S).** A "Clear filters" button, shown only
> when at least one filter is set. One `setSearchParams(new
> URLSearchParams())` call.

### 5.4 What do the empty and error states actually say?

| Situation | What renders | Verdict |
|---|---|---|
| Inventory pending | "Loading servers…", centred, muted (`InventoryPage.tsx:334`) | Fine; a skeleton table would hold layout better |
| Inventory zero rows | "No servers match the current filters." in a full-width cell (`InventoryTable.tsx:212`) | **Good** — names the cause and does not read as breakage. No "clear filters" action though (UX-5) |
| Inventory error | The `ApiError.problem.detail`, or `error.message` (`InventoryPage.tsx:339-347`) | Good text, but no `role="alert"` (F-11) and **no retry button** — the operator's only recourse is a browser reload |
| Sites pending | Six fixed-height skeleton cards, so the grid does not reflow (`SitesOverviewPage.tsx:251,311-326`) | **Best loading state in the app** |
| Sites error | "Could not load sites: {message}", `role="alert"` (`:298-306`) | Good. Note it renders *above* the skeleton grid, which still shows — mildly confusing |
| Site with zero servers | "empty" (`:242`) | Good — a site that exists and is empty is distinguishable from one that does not exist, which `e2e/sites.spec.ts:12-19` asserts on purpose |
| Detail pending | "Loading server…" (`ServerDetailPage.tsx:39`) | Fine |
| Detail error | Problem detail (`:41-49`) | No `role="alert"`, no retry |
| **Maintenance write fails** | **Nothing** | **F-02 — the one silent failure in the app** |
| Hardware not read | "Not reported" + tooltip | Excellent (section 4) |
| Network / connectivity empty | "No network interfaces." / "No connectivity data." | Confident zeros the frontend cannot currently qualify (section 4) |
| Rules/policies empty | "No classification rules are configured." | Good |

No route has an `errorElement`; there is no error boundary anywhere. A
throw in any component blanks the entire application, which is exactly
the `1a896af` failure mode.

> **UX-6 (proposal, effort S).** Add a `errorElement` on the layout route
> rendering "Something went wrong" plus a reload button, and a "Retry"
> button on the two query-error paragraphs (`refetch()` is already
> available from every `useQuery`).

### 5.5 Keyboard operation of the inventory table

**Workable today, with real gaps.** Walked through explicitly:

- **Filters** — all six are native `<input>`/`<select>`/checkbox, fully
  keyboard-operable, with a visible `focus-visible` ring in
  `FIELD_CLASS` (`InventoryPage.tsx:24`). But their accessible names are
  corrupted (F-06).
- **Sorting** — headers are real `<button>`s
  (`InventoryTable.tsx:140`) with a focus ring. Operable. But no
  `aria-sort` (F-10).
- **Rows** — each row's Name cell is a real `<a>`
  (`InventoryTable.tsx:76`), so Tab reaches every row, Enter opens it,
  and Ctrl/middle-click opens a new tab. The `<tr>`'s own `onClick` is
  explicitly documented as a convenience layered on top, and it defers to
  the anchor and to modified clicks (`:174-185`). **This is the right
  design and it is well executed.** The `<tr>`'s focus classes are dead
  (F-22).
- **Pagination** — real buttons with correct `disabled` states.
- **Detail tabs** — `<button>`s, Tab + Enter works. No arrow-key
  roving-tabindex, but they are not marked as a tablist either, so
  nothing promises it (F-05).

Missing: **no skip link** past the nav; **no focus management on route
change** — after clicking a row, focus stays where the anchor was and a
screen-reader user is not told the page changed; **no live region** on
filter results (F-11).

> **UX-7 (proposal, effort S).** A skip link, plus a focus-and-announce
> on route change (move focus to the `<h1>` and add an `aria-live`
> route-change announcer). Standard SPA pattern, ~20 lines.

---

## 6. What is already good

Said plainly, because a lot of this is above the norm:

1. **The `types/server.ts` module docstring** (`:8-18`) — the `X | null`
   vs `X | undefined` rule, derived from a real crash, with the commit
   hash. This is how a hard-won fact should be written down.
2. **`unread_fields` in `HardwareTab`** — the three-way not-read /
   carried-forward / read distinction is exactly right, tested by name,
   and better than most production tools manage.
3. **The severity design system** — distinct glyph + word + colour, one
   source of truth in `severity.ts`, with the two prior mistakes
   (identical glyphs, diamond-vs-triangle drift) recorded and asserted
   against.
4. **`index.css:1-135`** — a real `@theme` block. oklch with L held
   constant across status hues, a full dark override that redefines the
   tints rather than dimming them, a documented `prefers-reduced-motion`
   block that removes movement but keeps opacity/colour feedback,
   `tabular-nums` for numeric columns. The reasoning is written down and
   is correct.
5. **URL as filter state.** Filters, sort and cursor all shareable and
   refresh-safe, with the cursor correctly invalidated on filter change.
6. **The row-link pattern** (`InventoryTable.tsx:69-82`, `:174-185`) —
   real anchor first, row click as a deferential convenience, modified
   clicks untouched. Frequently got wrong; got right here.
7. **Facet counts on filter options**, with the discipline to stay silent
   rather than show a number that would be wrong.
8. **TypeScript rigour.** `noUncheckedIndexedAccess` +
   `exactOptionalPropertyTypes` + zero `any`, zero `!`, zero
   `@ts-ignore`. `InventoryPage.tsx:77-80` even explains why the params
   object is built incrementally instead of with `|| undefined`.
9. **The E2E suite has no flaky patterns.** Zero `waitForTimeout`. Role-
   and placeholder-based locators. `maintenance.spec.ts:36-41` documents
   precisely why `.click()` + a retrying assertion beats `.check()` for a
   router-driven checkbox — a real, correctly diagnosed race.
10. **`e2e/unread-fields.spec.ts`** picks its subject *from the live API*
    (the server with the most `unread_fields`) rather than from a fixture,
    and listens on `pageerror` because a React unmount is silent in the
    DOM. The comment at `:9-14` — "hand-written unit fixtures cannot cover
    this, because they are written from the same mental model of the
    payload that was wrong in the first place" — is the sharpest testing
    observation in the repo.
11. **`vite.config.ts:20-27`** — the `^/health/` regex proxy key, with the
    reasoning for why a plain prefix would swallow the SPA's own routes.

E2E coverage against the critical operator journeys: inventory list +
search + detail + every tab ✅; sites overview + drill-down to a
pre-filtered list ✅; maintenance enable/disable round trip + list filter
✅; rules page read-only + regex visible ✅; partially-read server renders
✅. **Not covered:** pagination (Next/Previous), sorting, the error state,
the empty state, keyboard-only operation, and — because it does not exist
— anything about per-category health.

---

## 7. Questions for the user

1. **UX-1 (per-category health on the Overview tab) is the single highest
   value/effort item found.** The data is fetched and discarded today.
   Approve?
2. **F-07 vs UX-3:** `HistoryPanel` + `listServerEvents` are dead code
   *and* are most of a per-server audit-history tab the backend already
   supports. Delete them, or wire them up?
3. **F-01** (a never-evaluated site reading "all healthy") — do you want
   `UNKNOWN` surfaced on the site card as its own count, or should "all
   healthy" simply require `HEALTHY === total`?
4. **F-13/F-14, the two colour systems:** worth a mechanical sweep of ~6
   files to put the server-detail subtree and `RulesPage` on the design
   tokens, or leave until those pages are next touched for a feature?
5. **F-08:** add runtime validation at the API boundary, or extend the
   backend-side contract test (`tests/unit/test_frontend_manager_types.py`)
   to cover the response *shapes* as well as the manager-type list?
6. **`/status`** still describes itself as a Phase-1 skeleton. Keep it as
   a debug route (reworded), or drop it?
7. **F-24 (nginx security headers):** does the OpenShift Route already
   set CSP / `nosniff` / `Referrer-Policy`? If not, they belong here.
8. **`page_size` is hardcoded to 50** (`InventoryPage.tsx:27`). Should an
   operator be able to choose 100/200 for a bulk scan?
