import { Link } from "react-router";

import { UNASSIGNED_SITE_ID } from "@/api/sites";
import type { SiteStats, VendorCount } from "@/api/sites";
import { SEVERITY_GLYPH } from "@/components/severity";
import { useSitesQuery } from "@/features/sites/hooks";
import type { HealthSeverity } from "@/types/server";

/**
 * The landing page: one card per site, each summarising what is in it,
 * plus a fleet-wide card summing them all.
 *
 * This is the entry point rather than the server list because at ~10,000
 * servers a flat list has no answer to "is anything wrong?" — you would
 * have to sort and scan. Cards answer it in one glance, and each one is a
 * link that pre-filters the list, so drilling in is one click and never
 * requires touching a filter control.
 */

const ACROSS_SITES_ID = "__all__";

const VENDOR_LABELS: Record<string, string> = {
  dell: "Dell",
  cisco: "Cisco",
  hp: "HP",
  standalone: "Standalone",
};

/** A vendor with no label of its own renders under its own name rather
 * than being dropped or lumped into an "Other" bucket — a vendor the UI
 * has never heard of is exactly the one worth seeing by name. */
function vendorLabel(vendor: string): string {
  return VENDOR_LABELS[vendor] ?? vendor;
}

/** The whole fleet as one `SiteStats`, summed from the per-site rows —
 * including `unassigned`, since those servers are in the fleet whatever
 * their hostname says. Derived here rather than served as a row of its
 * own so it can never disagree with the cards beside it. */
function acrossSites(items: SiteStats[]): SiteStats {
  const by_vendor: VendorCount[] = [];
  const by_health: Record<string, number> = {};
  let total = 0;
  let in_maintenance = 0;

  for (const site of items) {
    total += site.total;
    in_maintenance += site.in_maintenance;
    for (const entry of site.by_vendor) {
      const existing = by_vendor.find((v) => v.vendor === entry.vendor);
      if (existing) {
        existing.count += entry.count;
      } else {
        by_vendor.push({ ...entry });
      }
    }
    for (const [severity, count] of Object.entries(site.by_health)) {
      by_health[severity] = (by_health[severity] ?? 0) + count;
    }
  }

  return {
    site_id: ACROSS_SITES_ID,
    name: "Across all sites",
    total,
    by_vendor,
    by_health: by_health as Record<HealthSeverity, number>,
    in_maintenance,
  };
}

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
            <span className="w-16 shrink-0 text-[var(--text-secondary)]">
              {vendorLabel(entry.vendor)}
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
  const isUnassigned = site.site_id === UNASSIGNED_SITE_ID;
  const isAcrossSites = site.site_id === ACROSS_SITES_ID;

  return (
    <Link
      to={isAcrossSites ? "/servers" : `/servers?site_id=${site.site_id}`}
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
        {isUnassigned
          ? "no site in hostname"
          : isAcrossSites
            ? "servers, every site"
            : "servers"}
      </p>

      {/* Only surface counts that mean "look at this". A row of zeroes
       * across five cards is noise that trains people to skip the card. */}
      <div className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
        {critical > 0 && (
          <span className="inline-flex items-center gap-1.5 font-medium text-[var(--text-on-critical)]">
            <span aria-hidden="true">{SEVERITY_GLYPH.CRITICAL}</span>
            <span className="tabular">{critical}</span> critical
          </span>
        )}
        {warning > 0 && (
          <span className="inline-flex items-center gap-1.5 font-medium text-[var(--text-on-warning)]">
            <span aria-hidden="true">{SEVERITY_GLYPH.WARNING}</span>
            <span className="tabular">{warning}</span> warning
          </span>
        )}
        {site.in_maintenance > 0 && (
          <span className="inline-flex items-center gap-1.5 text-[var(--text-on-maintenance)]">
            <span aria-hidden="true">⏸</span>
            <span className="tabular">{site.in_maintenance}</span> in
            maintenance
          </span>
        )}
        {critical === 0 && warning === 0 && site.total > 0 && (
          <span className="inline-flex items-center gap-1.5 text-[var(--text-on-healthy)]">
            <span aria-hidden="true">{SEVERITY_GLYPH.HEALTHY}</span> all healthy
          </span>
        )}
        {site.total === 0 && (
          <span className="text-[var(--text-muted)]">empty</span>
        )}
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
  const { data, isPending, isError, error } = useSitesQuery();

  const cards = data ? [acrossSites(data.items), ...data.items] : [];
  const fleetTotal = cards[0]?.total ?? 0;

  return (
    <main className="mx-auto max-w-7xl px-8 py-8">
      <header className="mb-6">
        <h1 className="text-xl font-semibold tracking-tight text-[var(--text-primary)]">
          Sites
        </h1>
        <p className="mt-1 text-sm text-[var(--text-secondary)]">
          {isPending ? (
            "Loading fleet…"
          ) : (
            <>
              <span className="tabular font-medium text-[var(--text-primary)]">
                {fleetTotal.toLocaleString()}
              </span>{" "}
              servers across{" "}
              {data?.items.filter((s) => s.site_id !== UNASSIGNED_SITE_ID)
                .length ?? 0}{" "}
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
          Could not load sites:{" "}
          {error instanceof Error ? error.message : "unknown error"}
        </p>
      )}

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {isPending
          ? // Fixed-height placeholders matching the real card, so the grid
            // does not reflow when data lands.
            [0, 1, 2, 3, 4, 5].map((i) => <SkeletonCard key={i} />)
          : cards.map((site) => <SiteCard key={site.site_id} site={site} />)}
      </div>
    </main>
  );
}
