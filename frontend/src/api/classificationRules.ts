import { apiFetch } from "@/api/client";
import type {
  ClassificationPreviewRequest,
  ClassificationPreviewResponse,
  ClassificationRuleCreate,
  ClassificationRuleListResponse,
  ClassificationRuleResponse,
  ClassificationRuleUpdate,
} from "@/types/classification";

const BASE = "/api/v1/classification-rules";

export interface ClassificationRuleListParams {
  enabled?: boolean;
}

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

export function getClassificationRule(id: string): Promise<ClassificationRuleResponse> {
  return apiFetch<ClassificationRuleResponse>(`${BASE}/${encodeURIComponent(id)}`);
}

export function createClassificationRule(
  body: ClassificationRuleCreate,
): Promise<ClassificationRuleResponse> {
  return apiFetch<ClassificationRuleResponse>(BASE, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function updateClassificationRule(
  id: string,
  body: ClassificationRuleUpdate,
): Promise<ClassificationRuleResponse> {
  return apiFetch<ClassificationRuleResponse>(`${BASE}/${encodeURIComponent(id)}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}

export function deleteClassificationRule(id: string): Promise<void> {
  return apiFetch<void>(`${BASE}/${encodeURIComponent(id)}`, { method: "DELETE" });
}

export function previewClassificationRule(
  body: ClassificationPreviewRequest,
): Promise<ClassificationPreviewResponse> {
  return apiFetch<ClassificationPreviewResponse>(`${BASE}/preview`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}
