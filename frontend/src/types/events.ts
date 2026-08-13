/**
 * Hand-written types mirroring `/api/v1/events` and
 * `/api/v1/servers/{id}/events` (see `backend/app/api/v1/events_schemas.py`,
 * authoritative).
 */

export type ActorType = "SYSTEM" | "USER" | "TOKEN";

export interface Actor {
  type: ActorType;
  id: string;
  display: string | null;
}

/** The event types this slice's history panels care about. `event_type` on
 * the wire is a free string (the backend's `EventType` enum has more
 * members than these), so this is a UI-side subset, not the full type. */
export type ClassificationEventType =
  | "CLASSIFICATION_RULE_CREATED"
  | "CLASSIFICATION_RULE_UPDATED"
  | "CLASSIFICATION_RULE_DELETED";

export type HealthPolicyEventType =
  | "HEALTH_POLICY_CREATED"
  | "HEALTH_POLICY_UPDATED"
  | "HEALTH_POLICY_DISABLED"
  | "HEALTH_POLICY_DELETED";

export const CLASSIFICATION_EVENT_TYPES: ClassificationEventType[] = [
  "CLASSIFICATION_RULE_CREATED",
  "CLASSIFICATION_RULE_UPDATED",
  "CLASSIFICATION_RULE_DELETED",
];

export const HEALTH_POLICY_EVENT_TYPES: HealthPolicyEventType[] = [
  "HEALTH_POLICY_CREATED",
  "HEALTH_POLICY_UPDATED",
  "HEALTH_POLICY_DISABLED",
  "HEALTH_POLICY_DELETED",
];

export interface AuditEventResponse {
  id: string;
  event_type: string;
  server_id: string | null;
  actor: Actor;
  request_id: string | null;
  created_at: string;
  data: Record<string, unknown>;
}

export interface EventPageInfo {
  next_cursor: string | null;
  has_more: boolean;
  page_size: number;
}

export interface AuditEventListResponse {
  items: AuditEventResponse[];
  page: EventPageInfo;
}
