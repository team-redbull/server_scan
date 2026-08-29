import { apiFetch } from "@/api/client";
import type { HealthSeverity, SiteCode, Vendor } from "@/types/server";

/**
 * `GET /api/v1/sites` — the fixed site list with per-site statistics.
 *
 * The backend always returns every site in `SiteCode` plus an
 * `"unassigned"` bucket, in a stable order, whether or not any server
 * currently reports one. So this never needs to be merged against a
 * separate list of "known sites": the response IS the list.
 */

/** A site card's id: one of the five fixed sites, or the bucket for
 * servers whose name carries no site token. */
export type SiteStatsId = SiteCode | "unassigned";

export interface VendorCount {
  vendor: Vendor;
  count: number;
}

export interface SiteStats {
  site_id: SiteStatsId;
  name: string;
  total: number;
  by_vendor: VendorCount[];
  /** Always contains every `HealthSeverity` key, including zeroes. */
  by_health: Record<HealthSeverity, number>;
  in_maintenance: number;
}

export interface SiteStatsListResponse {
  items: SiteStats[];
}

export function listSites(): Promise<SiteStatsListResponse> {
  return apiFetch<SiteStatsListResponse>("/api/v1/sites");
}

/** The real sites, for filter dropdowns. Derived from the same
 * response the overview renders, so a site can never appear in one and
 * not the other. */
export const SITE_CODES: readonly SiteCode[] = ["nyc", "tlv", "bat-yam", "five"];

export const VENDORS: readonly Vendor[] = ["dell", "cisco", "hp", "standalone"];

/** How a server is reached, which is a different question from who built
 * it. A Dell reached at its own BMC is still `vendor: "dell"`; what makes
 * it unmanaged is `source_provider: "REDFISH_STANDALONE"`. Values match
 * the backend's `ManagerType`. */
export const SOURCE_PROVIDERS: readonly { value: string; label: string }[] = [
  { value: "UCS_CENTRAL", label: "UCS Central" },
  { value: "REDFISH_STANDALONE", label: "Standalone (Redfish)" },
];
