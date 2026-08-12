import { useMemo, useState } from "react";
import { useSearchParams } from "react-router";

import { ApiError } from "@/api/client";
import type { ServerListParams } from "@/api/servers";
import type { SortableField } from "@/features/inventory/InventoryTable";
import { InventoryTable } from "@/features/inventory/InventoryTable";
import { useServersQuery } from "@/features/inventory/hooks";
import { useDebouncedValue } from "@/lib/useDebouncedValue";

const VENDORS = ["dell", "cisco", "hpe", "unknown"] as const;
const INSTALLATION_TYPES = ["HOSTED_CLUSTER", "UPI", "UNCLASSIFIED"] as const;
const HEALTH_SEVERITIES = ["UNKNOWN", "HEALTHY", "INFO", "WARNING", "CRITICAL"] as const;
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
 * `site_id` is a free-text filter for this slice rather than a `<select>`
 * sourced from a sites endpoint — there's no `GET /sites` list in the slice
 * 1 contract to populate one from, so a dropdown would just be a hardcoded
 * guess. Free text against the exact `site_id` the backend stores is more
 * honest here and gets swapped for a real picker once a sites API exists.
 */
export function InventoryPage() {
  const [searchParams, setSearchParams] = useSearchParams();

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
  const maintenanceOnly = searchParams.get("maintenance") === "true";
  const sortParam = searchParams.get("sort") ?? "";
  const sortField: SortableField = isSortableField(sortParam) ? sortParam : DEFAULT_SORT;
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
    installationType,
    healthOverall,
    maintenanceOnly,
    sortField,
    sortDesc,
    cursor,
  ]);

  const { data, isPending, isError, error, isFetching } = useServersQuery(queryParams);

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
    <main className="mx-auto max-w-7xl p-8">
      <h1 className="text-2xl font-semibold">Server Inventory</h1>
      <p className="mt-1 text-sm text-gray-500">
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
        <label className="flex flex-col text-xs font-medium text-gray-500">
          Search
          <input
            type="text"
            value={searchInput}
            onChange={(e) => {
              updateFilters({ search: e.target.value });
            }}
            placeholder="Name, serial, tag…"
            className="mt-1 rounded border border-gray-300 px-2 py-1 text-sm dark:border-gray-600 dark:bg-gray-900"
          />
        </label>

        <label className="flex flex-col text-xs font-medium text-gray-500">
          Vendor
          <select
            value={vendor}
            onChange={(e) => {
              updateFilters({ vendor: e.target.value });
            }}
            className="mt-1 rounded border border-gray-300 px-2 py-1 text-sm dark:border-gray-600 dark:bg-gray-900"
          >
            <option value="">All</option>
            {VENDORS.map((v) => (
              <option key={v} value={v}>
                {v}
              </option>
            ))}
          </select>
        </label>

        <label className="flex flex-col text-xs font-medium text-gray-500">
          Site
          <input
            type="text"
            value={siteId}
            onChange={(e) => {
              updateFilters({ site_id: e.target.value });
            }}
            placeholder="site_..."
            className="mt-1 rounded border border-gray-300 px-2 py-1 text-sm dark:border-gray-600 dark:bg-gray-900"
          />
        </label>

        <label className="flex flex-col text-xs font-medium text-gray-500">
          Classification
          <select
            value={installationType}
            onChange={(e) => {
              updateFilters({ installation_type: e.target.value });
            }}
            className="mt-1 rounded border border-gray-300 px-2 py-1 text-sm dark:border-gray-600 dark:bg-gray-900"
          >
            <option value="">All</option>
            {INSTALLATION_TYPES.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
        </label>

        <label className="flex flex-col text-xs font-medium text-gray-500">
          Health
          <select
            value={healthOverall}
            onChange={(e) => {
              updateFilters({ health_overall: e.target.value });
            }}
            className="mt-1 rounded border border-gray-300 px-2 py-1 text-sm dark:border-gray-600 dark:bg-gray-900"
          >
            <option value="">All</option>
            {HEALTH_SEVERITIES.map((h) => (
              <option key={h} value={h}>
                {h}
              </option>
            ))}
          </select>
        </label>

        <label className="flex items-center gap-2 pb-1.5 text-xs font-medium text-gray-500">
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
        {isPending && <p className="text-gray-500">Loading servers…</p>}

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
            {isFetching && <p className="mb-2 text-xs text-gray-400">Updating…</p>}
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
                className="rounded border border-gray-300 px-3 py-1.5 text-sm disabled:cursor-not-allowed disabled:opacity-40 dark:border-gray-600"
              >
                Previous
              </button>
              <button
                type="button"
                onClick={handleNext}
                disabled={!hasMore}
                className="rounded border border-gray-300 px-3 py-1.5 text-sm disabled:cursor-not-allowed disabled:opacity-40 dark:border-gray-600"
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
