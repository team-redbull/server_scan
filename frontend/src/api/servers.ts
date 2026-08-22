import { apiFetch } from "@/api/client";
import type { ServerDetail, ServerListResponse } from "@/types/server";

/**
 * Query params accepted by `GET /api/v1/servers`. All optional — omitted
 * keys are left out of the request entirely (see `buildSearchParams`)
 * rather than sent as empty strings, so the backend sees the same request
 * whether a filter was never touched or was cleared back to "all".
 */
export interface ServerListParams {
  search?: string;
  site_id?: string;
  vendor?: string;
  manager_id?: string;
  source_provider?: string;
  installation_type?: string;
  health_overall?: string;
  maintenance?: boolean;
  sort?: "name" | "serial" | "model" | "updated_at" | "last_seen_at";
  sort_desc?: boolean;
  cursor?: string;
  page_size?: number;
  with_count?: boolean;
}

function buildSearchParams(params: ServerListParams): URLSearchParams {
  const searchParams = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") {
      continue;
    }
    searchParams.set(key, String(value));
  }
  return searchParams;
}

export function listServers(params: ServerListParams = {}): Promise<ServerListResponse> {
  const query = buildSearchParams(params).toString();
  const path = query ? `/api/v1/servers?${query}` : "/api/v1/servers";
  return apiFetch<ServerListResponse>(path);
}

export function getServer(id: string): Promise<ServerDetail> {
  return apiFetch<ServerDetail>(`/api/v1/servers/${encodeURIComponent(id)}`);
}

export interface MaintenanceEnableRequest {
  reason?: string;
  ticket?: string;
  expected_end?: string;
}

// Both endpoints return the full `ServerDetail` (not a maintenance-only
// body) — mirrors the backend's own choice (see `maintenance_schemas.py`'s
// docstring): a caller toggling maintenance almost always wants the
// resulting server state, not just the maintenance sub-document.
export function enableMaintenance(
  id: string,
  body: MaintenanceEnableRequest,
): Promise<ServerDetail> {
  return apiFetch<ServerDetail>(`/api/v1/servers/${encodeURIComponent(id)}/maintenance`, {
    method: "PUT",
    body: JSON.stringify(body),
  });
}

export function disableMaintenance(id: string): Promise<ServerDetail> {
  return apiFetch<ServerDetail>(`/api/v1/servers/${encodeURIComponent(id)}/maintenance`, {
    method: "DELETE",
  });
}
