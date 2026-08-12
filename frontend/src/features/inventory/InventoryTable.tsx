import { flexRender } from "@tanstack/react-table";
import type { SortingState } from "@tanstack/react-table";
// @tanstack/react-table v9 replaced the v8 `useReactTable`/`createColumnHelper`
// hooks with a new feature-composition API (`useTable` + explicit
// `features`). The package ships a `/legacy` compatibility entry point
// (`useLegacyTable` / `legacyCreateColumnHelper`) that preserves the v8
// hook shape we use below — the officially supported migration path,
// not a workaround — so we build on that rather than the fully-new API.
import { legacyCreateColumnHelper, useLegacyTable } from "@tanstack/react-table/legacy";
import type { LegacyColumnDef } from "@tanstack/react-table/legacy";
import { Link } from "react-router";

import type { ServerListParams } from "@/api/servers";
import { Badge } from "@/components/Badge";
import { HealthBadge } from "@/components/HealthBadge";
import type { ConnectivityFacts, ServerSummary } from "@/types/server";

/** The subset of `ServerListParams["sort"]` this table exposes as a
 * clickable column header. The backend also accepts "serial", but there's
 * no visible "serial" column in this slice's summary row, so it's left out
 * of the UI surface (still a valid API param, just not reachable from here). */
export type SortableField = "name" | "model" | "updated_at";

interface InventoryTableProps {
  servers: ServerSummary[];
  sortField: NonNullable<ServerListParams["sort"]>;
  sortDesc: boolean;
  onSortChange: (field: SortableField, desc: boolean) => void;
}

function formatFabricSummary(facts: ConnectivityFacts): string {
  if (facts.fabric_paths_total === 0) {
    return "—";
  }
  return `${facts.fabric_paths_up}/${facts.fabric_paths_total} up`;
}

const columnHelper = legacyCreateColumnHelper<ServerSummary>();

// Columns have heterogeneous `TValue` (string, boolean, ConnectivityFacts…).
// TanStack Table's own docs recommend widening the array element type to
// `ColumnDef<TData, any>` for exactly this case — the alternative is a
// `TValue=unknown` array, which `exactOptionalPropertyTypes` then rejects
// on every column (each accessor's inferred `TValue` is narrower than
// `unknown`, e.g. `string`, and narrowing back out isn't sound structurally).
const columns: LegacyColumnDef<ServerSummary, any>[] = [
  columnHelper.accessor("name", {
    id: "name",
    header: "Name",
    cell: (info) => (
      <Link
        to={`/servers/${info.row.original.id}`}
        className="font-medium text-blue-600 hover:underline dark:text-blue-400"
      >
        {info.getValue()}
      </Link>
    ),
    enableSorting: true,
  }),
  columnHelper.accessor("vendor", {
    id: "vendor",
    header: "Vendor",
    enableSorting: false,
  }),
  columnHelper.accessor("model", {
    id: "model",
    header: "Model",
    enableSorting: true,
  }),
  columnHelper.accessor("site_id", {
    id: "site",
    header: "Site",
    enableSorting: false,
  }),
  columnHelper.accessor((row) => row.classification.installation_type, {
    id: "classification",
    header: "Classification",
    cell: (info) => <Badge>{info.getValue()}</Badge>,
    enableSorting: false,
  }),
  columnHelper.accessor((row) => row.health.overall, {
    id: "health",
    header: "Health",
    cell: (info) => <HealthBadge severity={info.getValue()} />,
    enableSorting: false,
  }),
  columnHelper.accessor((row) => row.maintenance.enabled, {
    id: "maintenance",
    header: "Maintenance",
    cell: (info) =>
      info.getValue() ? (
        <Badge tone="warning">Maintenance</Badge>
      ) : (
        <span className="text-gray-400 dark:text-gray-600">—</span>
      ),
    enableSorting: false,
  }),
  columnHelper.accessor((row) => row.connectivity.facts, {
    id: "fabric",
    header: "Fabric",
    cell: (info) => formatFabricSummary(info.getValue()),
    enableSorting: false,
  }),
  columnHelper.accessor("updated_at", {
    id: "updated_at",
    header: "Last updated",
    cell: (info) => new Date(info.getValue()).toLocaleString(),
    enableSorting: true,
  }),
];

export function InventoryTable({ servers, sortField, sortDesc, onSortChange }: InventoryTableProps) {
  const sorting: SortingState = [{ id: sortField, desc: sortDesc }];

  const table = useLegacyTable({
    data: servers,
    columns,
    state: { sorting },
    manualSorting: true,
    enableSortingRemoval: false,
    onSortingChange: (updater) => {
      const next = typeof updater === "function" ? updater(sorting) : updater;
      const first = next[0];
      if (!first) {
        return;
      }
      // Safe: the only columns with `enableSorting: true` above use ids
      // "name" | "model" | "updated_at", which is exactly `SortableField`.
      onSortChange(first.id as SortableField, first.desc);
    },
    getRowId: (row) => row.id,
  });

  return (
    <div className="overflow-x-auto rounded-lg border border-gray-200 dark:border-gray-700">
      <table className="min-w-full divide-y divide-gray-200 text-sm dark:divide-gray-700">
        <thead className="bg-gray-50 dark:bg-gray-800/50">
          {table.getHeaderGroups().map((headerGroup) => (
            <tr key={headerGroup.id}>
              {headerGroup.headers.map((header) => (
                <th
                  key={header.id}
                  scope="col"
                  className="px-3 py-2 text-left font-medium text-gray-500 dark:text-gray-400"
                >
                  {header.column.getCanSort() ? (
                    <button
                      type="button"
                      onClick={header.column.getToggleSortingHandler()}
                      className="inline-flex items-center gap-1 hover:text-gray-900 dark:hover:text-gray-100"
                    >
                      {flexRender(header.column.columnDef.header, header.getContext())}
                      {header.column.getIsSorted() === "asc" && <span aria-hidden="true">▲</span>}
                      {header.column.getIsSorted() === "desc" && <span aria-hidden="true">▼</span>}
                    </button>
                  ) : (
                    flexRender(header.column.columnDef.header, header.getContext())
                  )}
                </th>
              ))}
            </tr>
          ))}
        </thead>
        <tbody className="divide-y divide-gray-100 dark:divide-gray-800">
          {table.getRowModel().rows.map((row) => (
            <tr key={row.id}>
              {row.getVisibleCells().map((cell) => (
                <td key={cell.id} className="px-3 py-2">
                  {flexRender(cell.column.columnDef.cell, cell.getContext())}
                </td>
              ))}
            </tr>
          ))}
          {servers.length === 0 && (
            <tr>
              <td colSpan={columns.length} className="px-3 py-6 text-center text-gray-500">
                No servers match the current filters.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
