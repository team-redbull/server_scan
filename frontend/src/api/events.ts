import { apiFetch } from "@/api/client";
import type { AuditEventListResponse } from "@/types/events";

export interface EventListParams {
  server_id?: string;
  event_type?: string;
  actor_id?: string;
  cursor?: string;
  page_size?: number;
}

function buildSearchParams<T extends object>(params: T): URLSearchParams {
  const searchParams = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === "") {
      continue;
    }
    searchParams.set(key, String(value as string | number));
  }
  return searchParams;
}

/** `GET /api/v1/events`. No server-side filter on `data.rule_id` /
 * `data.policy_id` exists — only `server_id`/`event_type`/`actor_id` — so
 * "history for this rule/policy" is built by fetching per `event_type` and
 * filtering client-side on `item.data.rule_id`/`policy_id`. See the
 * feature history panels for that filtering. A documented, deliberate
 * scope choice at current audit-log volumes (hundreds to low thousands of
 * events), not something to add backend support for in this slice. */
export function listEvents(params: EventListParams = {}): Promise<AuditEventListResponse> {
  const query = buildSearchParams(params).toString();
  return apiFetch<AuditEventListResponse>(query ? `/api/v1/events?${query}` : "/api/v1/events");
}

export function listServerEvents(
  serverId: string,
  params: { cursor?: string; page_size?: number } = {},
): Promise<AuditEventListResponse> {
  const query = buildSearchParams(params).toString();
  const path = `/api/v1/servers/${encodeURIComponent(serverId)}/events`;
  return apiFetch<AuditEventListResponse>(query ? `${path}?${query}` : path);
}
