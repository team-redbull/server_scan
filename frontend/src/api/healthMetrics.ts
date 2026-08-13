import { apiFetch } from "@/api/client";
import type { HealthMetricListResponse } from "@/types/health";

/** The metric registry backing the health-policy condition builder's
 * metric picker. Small, static-per-deploy list — no query params. */
export function listHealthMetrics(): Promise<HealthMetricListResponse> {
  return apiFetch<HealthMetricListResponse>("/api/v1/health-metrics");
}
