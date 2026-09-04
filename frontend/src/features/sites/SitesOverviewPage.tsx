import { Link } from "react-router";

import { UNASSIGNED_SITE_ID } from "@/api/sites";
import type { Breakdown, SiteStats, VendorCount } from "@/api/sites";
import { SEVERITY_GLYPH } from "@/components/severity";
import { useSitesQuery } from "@/features/sites/hooks";
import type { HealthSeverity, InstallationType } from "@/types/server";

/**
 * The landing page: a fleet-wide row (everything, UPI, hosted cluster)
 * above one card per site, each summarising what is in it.
 *
 * This is the entry point rather than the server list because at ~10,000
 * servers a flat list has no answer to "is anything wrong?" — you would
 * have to sort and scan. Cards answer it in one glance, and each one is a
 * link that pre-filters the list, so drilling in is one click and never
 * requires touching a filter control.
 */

/** What one card renders: a name, the counts behind it, and where it
 * drills into. Every card on this page is the same component — the only
 * thing that differs between "the whole fleet", "the UPI fleet" and "Tel
 * Aviv" is which slice of the response was summed and which filter the
 * link carries. */
interface CardSpec {
  key: string;
  name: string;
  subtitle: string;
  to: string;
  stats: Breakdown;
}

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

/**
 * Sum any set of breakdowns into one.
 *
 * Used for both fleet-wide rows: "across all sites" sums the site
 * records, and each installation-type card sums that type's slice out of
 * every site. Derived here rather than served as extra rows of its own so
 * a total can never disagree with the cards it was summed from.
 *
 * Args:
 *   records: the breakdowns to add together.
 *
 * Returns:
 *   Breakdown: their element-wise sum.
 */
function sumBreakdowns(records: Breakdown[]): Breakdown {
  const by_vendor: VendorCount[] = [];
  const by_health: Record<string, number> = {};
  let total = 0;
  let in_maintenance = 0;

  for (const record of records) {
    total += record.total;
    in_maintenance += record.in_maintenance;
    for (const entry of record.by_vendor) {
      const existing = by_vendor.find((v) => v.vendor === entry.vendor);
      if (existing) {
        existing.count += entry.count;
      } else {
        by_vendor.push({ ...entry });
      }
    }
    for (const [severity, count] of Object.entries(record.by_health)) {
      by_health[severity] = (by_health[severity] ?? 0) + count;
    }
  }

  return {
    total,
    by_vendor,
    by_health: by_health as Record<HealthSeverity, number>,
    in_maintenance,
  };
}

/**
 * The three fleet-wide cards: everything, then each installation type.
 *
 * `unassigned` is included in all of them — those servers are in the
 * fleet whatever their hostname says.
 *
 * Args:
 *   items: the per-site records as returned by `GET /api/v1/sites`.
 *
 * Returns:
 *   CardSpec[]: the top row, in fixed order.
 */
function fleetCards(items: SiteStats[]): CardSpec[] {
  const slice = (type: InstallationType): Breakdown =>
    sumBreakdowns(items.map((site) => site.by_installation_type[type]));

  return [
    {
      key: "__all__",
      name: "Across all sites",
      subtitle: "servers, every site",
      to: "/servers",
      stats: sumBreakdowns(items),
    },
    {
      key: "UPI",
      name: "UPI",
      subtitle: "servers, every site",
      to: "/servers?installation_type=UPI",
      stats: slice("UPI"),
    },
    {
      key: "HOSTED_CLUSTER",
      name: "Hosted cluster",
      subtitle: "servers, every site",
      to: "/servers?installation_type=HOSTED_CLUSTER",
      stats: slice("HOSTED_CLUSTER"),
    },
  ];
}

/**
 * One card per configured site, in the order the backend returned them.
 *
 * Args:
 *   items: the per-site records as returned by `GET /api/v1/sites`.
 *
 * Returns:
 *   CardSpec[]: the per-site row.
 */
function siteCards(items: SiteStats[]): CardSpec[] {
  return items.map((site) => ({
    key: site.site_id,
    name: site.name,
    subtitle:
      site.site_id === UNASSIGNED_SITE_ID ? "no site in hostname" : "servers",
    to: `/servers?site_id=${site.site_id}`,
    stats: site,
  }));
}

/** Bar widths are proportional to the card's own total, not to the
 * largest card — each one answers "what is the mix HERE", and
 * normalising across cards would make a small site's mix unreadable. */
function VendorBar({ stats }: { stats: Breakdown }) {
  if (stats.total === 0) {
    return null;
  }
  return (
    <div className="mt-4 space-y-1.5">
      {stats.by_vendor.map((entry) => {
        const percent = Math.round((entry.count / stats.total) * 100);
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

/** `emphasis` marks the fleet-wide row. It is a different kind of thing
 * from a site — a stronger border says so without a second card design. */
function SiteCard({ card, emphasis }: { card: CardSpec; emphasis?: boolean }) {
  const { stats } = card;
  const critical = stats.by_health.CRITICAL;
  const warning = stats.by_health.WARNING;

  return (
    <Link
      to={card.to}
      className={`group block rounded-[var(--radius-card)] border bg-[var(--surface-raised)] p-5 transition-[border-color,transform] duration-[var(--duration-fast)] ease-[var(--ease-out-strong)] hover:border-[var(--border-strong)] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--color-status-info)] active:scale-[0.995] ${
        emphasis
          ? "border-[var(--border-strong)]"
          : "border-[var(--border-subtle)]"
      }`}
    >
      <div className="flex items-baseline justify-between gap-3">
        <h3 className="text-sm font-semibold tracking-tight text-[var(--text-primary)]">
          {card.name}
        </h3>
        <span className="tabular text-2xl font-semibold text-[var(--text-primary)]">
          {stats.total.toLocaleString()}
        </span>
      </div>

      <p className="mt-0.5 text-xs text-[var(--text-muted)]">{card.subtitle}</p>

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
        {stats.in_maintenance > 0 && (
          <span className="inline-flex items-center gap-1.5 text-[var(--text-on-maintenance)]">
            <span aria-hidden="true">⏸</span>
            <span className="tabular">{stats.in_maintenance}</span> in
            maintenance
          </span>
        )}
        {critical === 0 && warning === 0 && stats.total > 0 && (
          <span className="inline-flex items-center gap-1.5 text-[var(--text-on-healthy)]">
            <span aria-hidden="true">{SEVERITY_GLYPH.HEALTHY}</span> all healthy
          </span>
        )}
        {stats.total === 0 && (
          <span className="text-[var(--text-muted)]">empty</span>
        )}
      </div>

      <VendorBar stats={stats} />
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

function SectionHeading({ children }: { children: string }) {
  return (
    <h2 className="mb-3 text-xs font-medium tracking-wide text-[var(--text-muted)] uppercase">
      {children}
    </h2>
  );
}

export function SitesOverviewPage() {
  const { data, isPending, isError, error } = useSitesQuery();

  const fleet = data ? fleetCards(data.items) : [];
  const sites = data ? siteCards(data.items) : [];
  const fleetTotal = fleet[0]?.stats.total ?? 0;

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

      <section className="mb-8">
        <SectionHeading>Fleet</SectionHeading>
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {isPending
            ? // Fixed-height placeholders matching the real card, so the
              // grid does not reflow when data lands.
              [0, 1, 2].map((i) => <SkeletonCard key={i} />)
            : fleet.map((card) => (
                <SiteCard key={card.key} card={card} emphasis />
              ))}
        </div>
      </section>

      <section>
        <SectionHeading>Sites</SectionHeading>
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {isPending
            ? [0, 1, 2, 3, 4, 5].map((i) => <SkeletonCard key={i} />)
            : sites.map((card) => <SiteCard key={card.key} card={card} />)}
        </div>
      </section>
    </main>
  );
}
