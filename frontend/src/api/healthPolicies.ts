import { apiFetch } from "@/api/client";
import type {
  HealthPolicyCreate,
  HealthPolicyListResponse,
  HealthPolicyPreviewRequest,
  HealthPolicyPreviewResponse,
  HealthPolicyResponse,
  HealthPolicyUpdate,
} from "@/types/health";

const BASE = "/api/v1/health-policies";

export interface HealthPolicyListParams {
  enabled?: boolean;
}

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

export function getHealthPolicy(id: string): Promise<HealthPolicyResponse> {
  return apiFetch<HealthPolicyResponse>(`${BASE}/${encodeURIComponent(id)}`);
}

export function createHealthPolicy(body: HealthPolicyCreate): Promise<HealthPolicyResponse> {
  return apiFetch<HealthPolicyResponse>(BASE, { method: "POST", body: JSON.stringify(body) });
}

export function updateHealthPolicy(
  id: string,
  body: HealthPolicyUpdate,
): Promise<HealthPolicyResponse> {
  return apiFetch<HealthPolicyResponse>(`${BASE}/${encodeURIComponent(id)}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}

export function deleteHealthPolicy(id: string): Promise<void> {
  return apiFetch<void>(`${BASE}/${encodeURIComponent(id)}`, { method: "DELETE" });
}

export function previewHealthPolicy(
  body: HealthPolicyPreviewRequest,
): Promise<HealthPolicyPreviewResponse> {
  return apiFetch<HealthPolicyPreviewResponse>(`${BASE}/preview`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}
