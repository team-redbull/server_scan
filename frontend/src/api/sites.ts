import { apiFetch } from "@/api/client";
import type {
  HealthSeverity,
  InstallationType,
  SiteCode,
  Vendor,
} from "@/types/server";

/**
 * `GET /api/v1/sites` — the fixed site list with per-site statistics.
 *
 * The backend always returns every site in `SiteCode` plus an
 * `"unassigned"` bucket, in a stable order, whether or not any server
 * currently reports one. So this never needs to be merged against a
 * separate list of "known sites": the response IS the list.
 */

/** A site card's id: a site code, or `UNASSIGNED_SITE_ID` for the bucket
 * of servers whose name carries no site token. */
export type SiteStatsId = SiteCode;

/** The id of the bucket for servers whose name names no site. */
export const UNASSIGNED_SITE_ID = "unassigned";

export interface VendorCount {
  vendor: Vendor;
  count: number;
}

/** The counts one slice of the fleet reports — a whole site, or one
 * installation type within it. Both render through the same card. */
export interface Breakdown {
  total: number;
  by_vendor: VendorCount[];
  /** Always contains every `HealthSeverity` key, including zeroes. */
  by_health: Record<HealthSeverity, number>;
  in_maintenance: number;
}

export interface SiteStats extends Breakdown {
  site_id: SiteStatsId;
  name: string;
  /** Always contains every `InstallationType` key, including empty ones.
   * The fleet-wide UPI/hosted totals are summed from these rather than
   * served as rows of their own, so they can never disagree with the
   * per-site cards beside them. */
  by_installation_type: Record<InstallationType, Breakdown>;
}

export interface SiteStatsListResponse {
  items: SiteStats[];
}

export function listSites(): Promise<SiteStatsListResponse> {
  return apiFetch<SiteStatsListResponse>("/api/v1/sites");
}

/** The real sites, for filter dropdowns — read from the same response the
 * overview renders, so a site can never appear in one and not the other,
 * and renaming a site in the backend enum needs no frontend change.
 *
 * Args:
 *   items: the `SiteStats` rows as returned by `listSites`.
 */
export function siteOptions(
  items: SiteStats[] | undefined,
): { value: string; label: string }[] {
  return (items ?? [])
    .filter((site) => site.site_id !== UNASSIGNED_SITE_ID)
    .map((site) => ({ value: site.site_id, label: site.name }));
}

export const VENDORS: readonly Vendor[] = ["dell", "cisco", "hp", "standalone"];

/** How a server is reached, which is a different question from who built
 * it. A Dell reached at its own BMC is still `vendor: "dell"`; what makes
 * it unmanaged is `source_provider: "REDFISH_STANDALONE"`. Values match
 * the backend's `ManagerType`.
 *
 * Only the collectors that actually exist are listed — filtering by one
 * with no implementation would always return nothing. The two Cisco
 * entries partition the Cisco fleet rather than overlapping: UCS Central
 * owns the UCS-managed domains, Intersight owns the servers no UCS domain
 * does. `tests/unit/test_frontend_manager_types.py` fails the build if a
 * collector is added to the backend and not to this list. */
export const SOURCE_PROVIDERS: readonly { value: string; label: string }[] = [
  { value: "UCS_CENTRAL", label: "UCS Central" },
  { value: "INTERSIGHT", label: "Intersight" },
  { value: "REDFISH_STANDALONE", label: "Standalone (Redfish)" },
];
