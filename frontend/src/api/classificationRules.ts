import { apiFetch } from "@/api/client";
import type { ClassificationRuleListResponse } from "@/types/classification";

const BASE = "/api/v1/classification-rules";

export interface ClassificationRuleListParams {
  enabled?: boolean;
}

/** List the classification rules this deployment runs.
 *
 * Read-only on purpose. The rules ship with the platform and are seeded
 * at startup, so this client has no create/update/delete counterpart to
 * reach for — the backend still exposes them, but nothing in the UI
 * should be the thing that makes two installations classify differently.
 */
export function listClassificationRules(
  params: ClassificationRuleListParams = {},
): Promise<ClassificationRuleListResponse> {
  const query = new URLSearchParams();
  if (params.enabled !== undefined) {
    query.set("enabled", String(params.enabled));
  }
  const qs = query.toString();
  return apiFetch<ClassificationRuleListResponse>(qs ? `${BASE}?${qs}` : BASE);
}
