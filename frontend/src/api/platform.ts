import { apiFetch } from "@/api/client";

export interface ReadinessResponse {
  status: "ok" | "not_ready";
  dependencies: {
    mongo: "ok" | "unreachable";
    redis: "ok" | "degraded";
  };
}

export function getReadiness(): Promise<ReadinessResponse> {
  return apiFetch<ReadinessResponse>("/health/ready");
}
