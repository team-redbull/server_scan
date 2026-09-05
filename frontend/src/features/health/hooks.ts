import { useQuery } from "@tanstack/react-query";

import type { HealthPolicyListParams } from "@/api/healthPolicies";
import { listHealthPolicies } from "@/api/healthPolicies";
import { queryKeys } from "@/api/queryKeys";

/** Read-only by design, like the classification hooks next door: health
 * policies ship with the platform and are seeded at startup. */
export function useHealthPoliciesQuery(params: HealthPolicyListParams = {}) {
  return useQuery({
    queryKey: queryKeys.healthPolicies.list(params),
    queryFn: () => listHealthPolicies(params),
  });
}
