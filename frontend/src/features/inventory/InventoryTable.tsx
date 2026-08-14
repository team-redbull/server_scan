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
import { Link, useNavigate } from "react-router";

import type { ServerListParams } from "@/api/servers";
import { StateBadge } from "@/components/StateBadge";
import type { HealthSeverity } from "@/types/server";
import type { ServerSummary } from "@/types/server";

/**
 * Three columns, deliberately: name, model, state.
 *
 * The previous table had nine (vendor, site, classification, fabric,
 * last-updated…), which is a lot of horizontal scanning to answer the two
 * questions this screen exists for — "which box is this" and "does it need
 * me". Everything cut is still one click away on the detail page, where
 * there is room to present it properly. Site in particular is now
 * redundant in every row: it is already inside the hostname, and the list
 * is normally reached pre-filtered from a site card.
 *
 * Motion note: rows animate nothing. An operator scrolls this list many
 * times a day, and per-row transitions on a 50-row table are both a
 * distraction and a frame-budget cost at that repetition. Only the row
 * background responds to hover, which is instant feedback rather than
 * animation.
 */

/** The row accent for a severity: a 2px left edge on the rows that need
 * attention and nothing on the rest. Scanning fifty rows for a colour in
 * the middle of a table is slower than following one vertical edge, and
 * accenting every row (including healthy ones) would put the signal back
 * to zero. */
const ROW_ACCENT: Record<HealthSeverity, string> = {
  CRITICAL: "border-l-2 border-l-[var(--color-status-critical)]",
  WARNING: "border-l-2 border-l-[var(--color-status-warning)]",
  INFO: "border-l-2 border-l-transparent",
  HEALTHY: "border-l-2 border-l-transparent",
  UNKNOWN: "border-l-2 border-l-transparent",
};

export type SortableField = "name" | "model" | "updated_at";

interface InventoryTableProps {
  servers: ServerSummary[];
  sortField: NonNullable<ServerListParams["sort"]>;
  sortDesc: boolean;
  onSortChange: (field: SortableField, desc: boolean) => void;
}

const columnHelper = legacyCreateColumnHelper<ServerSummary>();

// Columns have heterogeneous `TValue` (string, ServerSummary…). TanStack
// Table's own docs recommend widening the array element type to
// `ColumnDef<TData, any>` for exactly this case — the alternative is a
// `TValue=unknown` array, which `exactOptionalPropertyTypes` then rejects
// on every column.
const columns: LegacyColumnDef<ServerSummary, any>[] = [
  columnHelper.accessor("name", {
    id: "name",
    header: "Name",
    cell: (info) => (
      // A real anchor, so ctrl/middle-click opens a server in a new tab
      // and assistive tech announces it as a link — the row's own
      // `onClick` is a convenience on top of this, never a replacement
      // for it. Styled as normal text rather than a blue underlined link:
      // with every row linked, per-row link colouring turns the column
      // into a wall of blue and stops signalling anything.
      <Link
        to={`/servers/${info.row.original.id}`}
        className="font-medium text-[var(--text-primary)] underline-offset-2 hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--color-status-info)]"
      >
        {info.getValue()}
      </Link>
    ),
    enableSorting: true,
  }),
  columnHelper.accessor("model", {
    id: "model",
    header: "Model",
    cell: (info) => (
      <span className="text-[var(--text-secondary)]">{info.getValue() || "—"}</span>
    ),
    enableSorting: true,
  }),
  columnHelper.accessor((row) => row, {
    id: "state",
    header: "State",
    cell: (info) => {
      const row = info.getValue<ServerSummary>();
      return <StateBadge severity={row.health.overall} maintenance={row.maintenance} />;
    },
    enableSorting: false,
  }),
];

export function InventoryTable({ servers, sortField, sortDesc, onSortChange }: InventoryTableProps) {
  const navigate = useNavigate();
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
      onSortChange(first.id as SortableField, first.desc);
    },
    getRowId: (row) => row.id,
  });

  return (
    <div className="overflow-hidden rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--surface-raised)]">
      <table className="min-w-full text-sm">
        {/* Sticky header: at 100+ rows the column meaning otherwise
         * scrolls away exactly when you are deep enough to need it. */}
        <thead className="sticky top-0 z-10 bg-[var(--surface-sunken)]">
          {table.getHeaderGroups().map((headerGroup) => (
            <tr key={headerGroup.id}>
              {headerGroup.headers.map((header) => (
                <th
                  key={header.id}
                  scope="col"
                  className="border-b border-[var(--border-subtle)] px-4 py-2.5 text-left text-xs font-medium tracking-wide text-[var(--text-secondary)] uppercase"
                >
                  {header.column.getCanSort() ? (
                    <button
                      type="button"
                      onClick={header.column.getToggleSortingHandler()}
                      // `uppercase` repeated here on purpose: Tailwind preflight sets
                      // `text-transform: none` on <button>, so a sortable header would
                      // otherwise render in a different case from a non-sortable one.
                      className="inline-flex items-center gap-1 rounded-sm uppercase hover:text-[var(--text-primary)] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--color-status-info)]"
                    >
                      {flexRender(header.column.columnDef.header, header.getContext())}
                      <span aria-hidden="true" className="text-[0.65rem]">
                        {header.column.getIsSorted() === "asc" && "▲"}
                        {header.column.getIsSorted() === "desc" && "▼"}
                      </span>
                    </button>
                  ) : (
                    flexRender(header.column.columnDef.header, header.getContext())
                  )}
                </th>
              ))}
              <th scope="col" className="w-6 border-b border-[var(--border-subtle)]">
                <span className="sr-only">Open</span>
              </th>
            </tr>
          ))}
        </thead>
        <tbody>
          {table.getRowModel().rows.map((row) => (
            // The whole row is a click target on top of the name link: a
            // single line of text is a poor target when aiming at one of
            // fifty rows. The name `<Link>` remains the accessible
            // primitive — this row handler defers to it for anything the
            // browser already handles natively.
            <tr
              key={row.id}
              onClick={(event) => {
                // Let the real anchor handle its own clicks, and never
                // hijack a modified click — ctrl/cmd/middle-click must
                // still open a new tab rather than navigating this one.
                if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.shiftKey) {
                  return;
                }
                if ((event.target as HTMLElement).closest("a")) {
                  return;
                }
                void navigate(`/servers/${row.original.id}`);
              }}
              className={`group cursor-pointer border-b border-[var(--border-subtle)] transition-colors duration-[var(--duration-instant)] ease-[var(--ease-out-strong)] last:border-0 hover:bg-[var(--surface-hover)] focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-[var(--color-status-info)] ${ROW_ACCENT[row.original.health.overall]}`}
            >
              {row.getVisibleCells().map((cell) => (
                <td key={cell.id} className="px-4 py-2.5 whitespace-nowrap">
                  {flexRender(cell.column.columnDef.cell, cell.getContext())}
                </td>
              ))}
              {/* Disclosure affordance: appears on hover so the row reads
                  as "leads somewhere" without adding permanent chrome to
                  every one of fifty rows. */}
              <td className="w-6 pr-3 text-right">
                <span
                  aria-hidden="true"
                  className="inline-block text-[var(--text-muted)] opacity-0 transition-opacity duration-[var(--duration-fast)] ease-[var(--ease-out-strong)] group-hover:opacity-100"
                >
                  ›
                </span>
              </td>
            </tr>
          ))}
          {servers.length === 0 && (
            <tr>
              <td
                colSpan={columns.length + 1}
                className="px-4 py-12 text-center text-sm text-[var(--text-muted)]"
              >
                No servers match the current filters.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
