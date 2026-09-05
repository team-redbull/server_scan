import { apiFetch } from "@/api/client";
import type { HealthPolicyListResponse } from "@/types/health";

const BASE = "/api/v1/health-policies";

export interface HealthPolicyListParams {
  enabled?: boolean;
}

/** List the health policies this deployment runs.
 *
 * Read-only for the same reason as the classification rules next door.
 */
export function listHealthPolicies(
  params: HealthPolicyListParams = {},
): Promise<HealthPolicyListResponse> {
  const query = new URLSearchParams();
  if (params.enabled !== undefined) {
    query.set("enabled", String(params.enabled));
  }
  const qs = query.toString();
  return apiFetch<HealthPolicyListResponse>(qs ? `${BASE}?${qs}` : BASE);
}
