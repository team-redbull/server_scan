import { useMemo, useState } from "react";
import { useSearchParams } from "react-router";

import { ApiError } from "@/api/client";
import type { ServerListParams } from "@/api/servers";
import type { SortableField } from "@/features/inventory/InventoryTable";
import { InventoryTable } from "@/features/inventory/InventoryTable";
import { siteOptions, SOURCE_PROVIDERS, VENDORS } from "@/api/sites";
import { useServersQuery } from "@/features/inventory/hooks";
import { useSitesQuery } from "@/features/sites/hooks";
import { useDebouncedValue } from "@/lib/useDebouncedValue";

const INSTALLATION_TYPES = ["HOSTED_CLUSTER", "UPI", "UNCLASSIFIED"] as const;
const HEALTH_SEVERITIES = [
  "UNKNOWN",
  "HEALTHY",
  "INFO",
  "WARNING",
  "CRITICAL",
] as const;
// One class string for every filter control so the bar reads as a single
// row of peers rather than a set of slightly different boxes.
const FIELD_CLASS =
  "mt-1 rounded-md border border-[var(--border-subtle)] bg-[var(--surface-raised)] px-2.5 py-1.5 text-sm text-[var(--text-primary)] focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-[var(--color-status-info)]";

const DEFAULT_SORT: SortableField = "name";
const PAGE_SIZE = 50;

function isSortableField(value: string): value is SortableField {
  return value === "name" || value === "model" || value === "updated_at";
}

/**
 * Primary landing page: the server inventory table. All filter and
 * pagination state lives in the URL (`useSearchParams`) rather than
 * component state, so a refresh or a back-button navigation lands the user
 * back where they were — an explicit project requirement, not polish.
 *
 * Site and vendor are both closed `<select>`s sourced from the domain's
 * own enums, not free text. The previous free-text `site_id` box required
 * typing an opaque generated id exactly right to get any result at all,
 * and a single wrong character returned an empty table that looked
 * identical to "this site has no servers".
 */
export function InventoryPage() {
  const [searchParams, setSearchParams] = useSearchParams();

  // The site dropdown's options come from the sites endpoint, never from a
  // list held here: the backend enum is the only definition of which sites
  // exist and what each is called.
  const sites = siteOptions(useSitesQuery().data?.items);

  // The backend only gives us a forward cursor, so "Previous" is backed by
  // a locally-tracked stack of visited cursors. It resets whenever filters
  // change (a new filter set invalidates the whole cursor chain) and does
  // not survive a page reload — an acceptable slice-1 limitation, since the
  // "Next" flow (the required behavior) is fully URL-backed regardless.
  const [cursorHistory, setCursorHistory] = useState<string[]>([]);

  const searchInput = searchParams.get("search") ?? "";
  const debouncedSearch = useDebouncedValue(searchInput, 300);

  const vendor = searchParams.get("vendor") ?? "";
  const siteId = searchParams.get("site_id") ?? "";
  const installationType = searchParams.get("installation_type") ?? "";
  const healthOverall = searchParams.get("health_overall") ?? "";
  const sourceProvider = searchParams.get("source_provider") ?? "";
  const maintenanceOnly = searchParams.get("maintenance") === "true";
  const sortParam = searchParams.get("sort") ?? "";
  const sortField: SortableField = isSortableField(sortParam)
    ? sortParam
    : DEFAULT_SORT;
  const sortDesc = searchParams.get("sort_desc") === "true";
  const cursor = searchParams.get("cursor") ?? undefined;

  const queryParams: ServerListParams = useMemo(() => {
    // Built incrementally (rather than `field: value || undefined`) because
    // `exactOptionalPropertyTypes` forbids assigning `undefined` to an
    // optional property outright — an omitted key and a key explicitly set
    // to `undefined` are distinct types under this tsconfig.
    const params: ServerListParams = { sort: sortField, page_size: PAGE_SIZE };
    if (debouncedSearch) params.search = debouncedSearch;
    if (vendor) params.vendor = vendor;
    if (siteId) params.site_id = siteId;
    if (sourceProvider) params.source_provider = sourceProvider;
    if (installationType) params.installation_type = installationType;
    if (healthOverall) params.health_overall = healthOverall;
    if (maintenanceOnly) params.maintenance = true;
    if (sortDesc) params.sort_desc = true;
    if (cursor) params.cursor = cursor;
    return params;
  }, [
    debouncedSearch,
    vendor,
    siteId,
    sourceProvider,
    installationType,
    healthOverall,
    maintenanceOnly,
    sortField,
    sortDesc,
    cursor,
  ]);

  const { data, isPending, isError, error, isFetching } =
    useServersQuery(queryParams);

  /** Apply a filter patch to the URL and drop any in-flight cursor — the
   * backend rejects a cursor from before a filter change, so the UI
   * shouldn't send one. `replace: true` keeps keystroke-level search edits
   * from spamming browser history. */
  function updateFilters(patch: Record<string, string | null>) {
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        for (const [key, value] of Object.entries(patch)) {
          if (value === null || value === "") {
            next.delete(key);
          } else {
            next.set(key, value);
          }
        }
        next.delete("cursor");
        return next;
      },
      { replace: true },
    );
    setCursorHistory([]);
  }

  function handleSortChange(field: SortableField, desc: boolean) {
    updateFilters({ sort: field, sort_desc: desc ? "true" : null });
  }

  function handleNext() {
    const nextCursor = data?.page.next_cursor;
    if (!nextCursor) {
      return;
    }
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        next.set("cursor", nextCursor);
        return next;
      },
      { replace: true },
    );
    setCursorHistory((prev) => [...prev, cursor ?? ""]);
  }

  function handlePrevious() {
    const history = [...cursorHistory];
    const previousCursor = history.pop();
    setCursorHistory(history);
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        if (previousCursor) {
          next.set("cursor", previousCursor);
        } else {
          next.delete("cursor");
        }
        return next;
      },
      { replace: true },
    );
  }

  const servers = data?.items ?? [];
  const hasMore = data?.page.has_more ?? false;

  return (
    <main className="mx-auto max-w-7xl px-8 py-8">
      <h1 className="text-xl font-semibold tracking-tight text-[var(--text-primary)]">
        Servers
      </h1>
      <p className="mt-1 text-sm text-[var(--text-secondary)]">
        {typeof data?.page.count === "number"
          ? `${data.page.count} server${data.page.count === 1 ? "" : "s"}`
          : "Browse and filter discovered servers."}
      </p>

      <form
        className="mt-6 flex flex-wrap items-end gap-3"
        onSubmit={(e) => {
          e.preventDefault();
        }}
      >
        <label className="flex flex-col text-xs font-medium text-[var(--text-secondary)]">
          Search
          <input
            type="text"
            value={searchInput}
            onChange={(e) => {
              updateFilters({ search: e.target.value });
            }}
            placeholder="Name, serial, tag…"
            className={FIELD_CLASS}
          />
        </label>

        <label className="flex flex-col text-xs font-medium text-[var(--text-secondary)]">
          Vendor
          <select
            value={vendor}
            onChange={(e) => {
              updateFilters({ vendor: e.target.value });
            }}
            className={FIELD_CLASS}
          >
            <option value="">All</option>
            {VENDORS.map((v) => (
              <option key={v} value={v}>
                {v}
              </option>
            ))}
          </select>
        </label>

        {/* How a server is reached, which is a different question from who
            built it. `REDFISH_STANDALONE` means the machine has no manager,
            so there is no point looking for it in OpenManage or UCS. */}
        <label className="flex flex-col text-xs font-medium text-[var(--text-secondary)]">
          Source
          <select
            value={sourceProvider}
            onChange={(e) => {
              updateFilters({ source_provider: e.target.value });
            }}
            className={FIELD_CLASS}
          >
            <option value="">All</option>
            {SOURCE_PROVIDERS.map((s) => (
              <option key={s.value} value={s.value}>
                {s.label}
              </option>
            ))}
          </select>
        </label>

        <label className="flex flex-col text-xs font-medium text-[var(--text-secondary)]">
          Site
          <select
            value={siteId}
            onChange={(e) => {
              updateFilters({ site_id: e.target.value });
            }}
            className={FIELD_CLASS}
          >
            <option value="">All sites</option>
            {sites.map((site) => (
              <option key={site.value} value={site.value}>
                {site.label}
              </option>
            ))}
          </select>
        </label>

        <label className="flex flex-col text-xs font-medium text-[var(--text-secondary)]">
          Classification
          <select
            value={installationType}
            onChange={(e) => {
              updateFilters({ installation_type: e.target.value });
            }}
            className={FIELD_CLASS}
          >
            <option value="">All</option>
            {INSTALLATION_TYPES.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
        </label>

        <label className="flex flex-col text-xs font-medium text-[var(--text-secondary)]">
          Health
          <select
            value={healthOverall}
            onChange={(e) => {
              updateFilters({ health_overall: e.target.value });
            }}
            className={FIELD_CLASS}
          >
            <option value="">All</option>
            {HEALTH_SEVERITIES.map((h) => (
              <option key={h} value={h}>
                {h}
              </option>
            ))}
          </select>
        </label>

        <label className="flex items-center gap-2 pb-1.5 text-xs font-medium text-[var(--text-secondary)]">
          <input
            type="checkbox"
            checked={maintenanceOnly}
            onChange={(e) => {
              updateFilters({ maintenance: e.target.checked ? "true" : null });
            }}
          />
          Maintenance only
        </label>
      </form>

      <div className="mt-4">
        {isPending && (
          <p className="py-12 text-center text-sm text-[var(--text-muted)]">
            Loading servers…
          </p>
        )}

        {isError && (
          <p className="rounded border border-red-300 bg-red-50 p-3 text-red-700 dark:border-red-800 dark:bg-red-950 dark:text-red-300">
            {error instanceof ApiError
              ? error.problem.detail
              : error instanceof Error
                ? error.message
                : "Failed to load servers."}
          </p>
        )}

        {!isPending && !isError && (
          <>
            {isFetching && (
              <p className="mb-2 text-xs text-gray-400">Updating…</p>
            )}
            <InventoryTable
              servers={servers}
              sortField={sortField}
              sortDesc={sortDesc}
              onSortChange={handleSortChange}
            />

            <div className="mt-4 flex items-center gap-3">
              <button
                type="button"
                onClick={handlePrevious}
                disabled={cursorHistory.length === 0}
                className="rounded-md border border-[var(--border-subtle)] bg-[var(--surface-raised)] px-3 py-1.5 text-sm text-[var(--text-primary)] transition-transform duration-[var(--duration-instant)] ease-[var(--ease-out-strong)] hover:border-[var(--border-strong)] active:scale-[0.97] disabled:cursor-not-allowed disabled:opacity-40 disabled:active:scale-100"
              >
                Previous
              </button>
              <button
                type="button"
                onClick={handleNext}
                disabled={!hasMore}
                className="rounded-md border border-[var(--border-subtle)] bg-[var(--surface-raised)] px-3 py-1.5 text-sm text-[var(--text-primary)] transition-transform duration-[var(--duration-instant)] ease-[var(--ease-out-strong)] hover:border-[var(--border-strong)] active:scale-[0.97] disabled:cursor-not-allowed disabled:opacity-40 disabled:active:scale-100"
              >
                Next
              </button>
            </div>
          </>
        )}
      </div>
    </main>
  );
}
