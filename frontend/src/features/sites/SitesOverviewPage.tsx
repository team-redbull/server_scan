import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router";

import { listSites } from "@/api/sites";
import type { SiteStats } from "@/api/sites";
import { queryKeys } from "@/api/queryKeys";

/**
 * The landing page: five sites, each summarising what is in it.
 *
 * This is the entry point rather than the server list because at ~10,000
 * servers a flat list has no answer to "is anything wrong?" — you would
 * have to sort and scan. Five cards answer it in one glance, and each one
 * is a link that pre-filters the list, so drilling in is one click and
 * never requires touching a filter control.
 */

const VENDOR_LABELS: Record<string, string> = {
  dell: "Dell",
  cisco: "Cisco",
  hp: "HP",
};

/** Bar widths are proportional to the site's own total, not to the
 * largest site — each card answers "what is the mix HERE", and
 * normalising across cards would make a small site's mix unreadable. */
function VendorBar({ site }: { site: SiteStats }) {
  if (site.total === 0) {
    return null;
  }
  return (
    <div className="mt-4 space-y-1.5">
      {site.by_vendor.map((entry) => {
        const percent = Math.round((entry.count / site.total) * 100);
        return (
          <div key={entry.vendor} className="flex items-center gap-2.5 text-xs">
            <span className="w-10 shrink-0 text-[var(--text-secondary)]">
              {VENDOR_LABELS[entry.vendor] ?? entry.vendor}
            </span>
            <span
              className="h-1.5 flex-1 overflow-hidden rounded-full bg-[var(--surface-sunken)]"
              aria-hidden="true"
            >
              <span
                className="block h-full rounded-full bg-[var(--border-strong)]"
                style={{ width: `${percent}%` }}
              />
            </span>
            <span className="tabular w-9 shrink-0 text-right text-[var(--text-secondary)]">
              {entry.count}
            </span>
          </div>
        );
      })}
    </div>
  );
}

function SiteCard({ site }: { site: SiteStats }) {
  const critical = site.by_health.CRITICAL;
  const warning = site.by_health.WARNING;
  const isUnassigned = site.site_id === "unassigned";

  return (
    <Link
      to={`/servers?site_id=${site.site_id}`}
      className="group block rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--surface-raised)] p-5 transition-[border-color,transform] duration-[var(--duration-fast)] ease-[var(--ease-out-strong)] hover:border-[var(--border-strong)] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--color-status-info)] active:scale-[0.995]"
    >
      <div className="flex items-baseline justify-between gap-3">
        <h2 className="text-sm font-semibold tracking-tight text-[var(--text-primary)]">
          {site.name}
        </h2>
        <span className="tabular text-2xl font-semibold text-[var(--text-primary)]">
          {site.total.toLocaleString()}
        </span>
      </div>

      <p className="mt-0.5 text-xs text-[var(--text-muted)]">
        {isUnassigned ? "no site in hostname" : "servers"}
      </p>

      {/* Only surface counts that mean "look at this". A row of zeroes
       * across five cards is noise that trains people to skip the card. */}
      <div className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
        {critical > 0 && (
          <span className="inline-flex items-center gap-1.5 font-medium text-[var(--text-on-critical)]">
            <span aria-hidden="true">▲</span>
            <span className="tabular">{critical}</span> critical
          </span>
        )}
        {warning > 0 && (
          <span className="inline-flex items-center gap-1.5 font-medium text-[var(--text-on-warning)]">
            <span aria-hidden="true">◆</span>
            <span className="tabular">{warning}</span> warning
          </span>
        )}
        {site.in_maintenance > 0 && (
          <span className="inline-flex items-center gap-1.5 text-[var(--text-on-maintenance)]">
            <span aria-hidden="true">⏸</span>
            <span className="tabular">{site.in_maintenance}</span> in maintenance
          </span>
        )}
        {critical === 0 && warning === 0 && site.total > 0 && (
          <span className="inline-flex items-center gap-1.5 text-[var(--text-on-healthy)]">
            <span aria-hidden="true">●</span> all healthy
          </span>
        )}
        {site.total === 0 && <span className="text-[var(--text-muted)]">empty</span>}
      </div>

      <VendorBar site={site} />
    </Link>
  );
}

function SkeletonCard() {
  return (
    <div
      className="h-[184px] rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--surface-raised)]"
      aria-hidden="true"
    />
  );
}

export function SitesOverviewPage() {
  const { data, isPending, isError, error } = useQuery({
    queryKey: queryKeys.sites.list(),
    queryFn: () => listSites(),
  });

  const fleetTotal = data?.items.reduce((sum, site) => sum + site.total, 0) ?? 0;

  return (
    <main className="mx-auto max-w-7xl px-8 py-8">
      <header className="mb-6">
        <h1 className="text-xl font-semibold tracking-tight text-[var(--text-primary)]">Sites</h1>
        <p className="mt-1 text-sm text-[var(--text-secondary)]">
          {isPending ? (
            "Loading fleet…"
          ) : (
            <>
              <span className="tabular font-medium text-[var(--text-primary)]">
                {fleetTotal.toLocaleString()}
              </span>{" "}
              servers across {data?.items.filter((s) => s.site_id !== "unassigned").length ?? 0}{" "}
              sites
            </>
          )}
        </p>
      </header>

      {isError && (
        <p
          role="alert"
          className="rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--tint-critical)] px-4 py-3 text-sm text-[var(--text-on-critical)]"
        >
          Could not load sites: {error instanceof Error ? error.message : "unknown error"}
        </p>
      )}

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {isPending
          ? // Fixed-height placeholders matching the real card, so the grid
            // does not reflow when data lands.
            [0, 1, 2, 3, 4, 5].map((i) => <SkeletonCard key={i} />)
          : data?.items.map((site) => <SiteCard key={site.site_id} site={site} />)}
      </div>
    </main>
  );
}
