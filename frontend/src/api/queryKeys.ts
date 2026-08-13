import type { ClassificationRuleListParams } from "@/api/classificationRules";
import type { EventListParams } from "@/api/events";
import type { HealthPolicyListParams } from "@/api/healthPolicies";
import type { ServerListParams } from "@/api/servers";

/**
 * Central TanStack Query key factory. Keeps key shape/order consistent
 * across hooks so invalidation (`queryClient.invalidateQueries`) can target
 * a whole resource (`queryKeys.servers.all`) or a narrower slice
 * (`queryKeys.servers.lists()`) without every call site hand-rolling arrays.
 */
export const queryKeys = {
  servers: {
    all: ["servers"] as const,
    lists: () => [...queryKeys.servers.all, "list"] as const,
    list: (params: ServerListParams) => [...queryKeys.servers.lists(), params] as const,
    details: () => [...queryKeys.servers.all, "detail"] as const,
    detail: (id: string) => [...queryKeys.servers.details(), id] as const,
  },
  classificationRules: {
    all: ["classificationRules"] as const,
    lists: () => [...queryKeys.classificationRules.all, "list"] as const,
    list: (params: ClassificationRuleListParams) =>
      [...queryKeys.classificationRules.lists(), params] as const,
    details: () => [...queryKeys.classificationRules.all, "detail"] as const,
    detail: (id: string) => [...queryKeys.classificationRules.details(), id] as const,
    preview: () => [...queryKeys.classificationRules.all, "preview"] as const,
  },
  healthPolicies: {
    all: ["healthPolicies"] as const,
    lists: () => [...queryKeys.healthPolicies.all, "list"] as const,
    list: (params: HealthPolicyListParams) => [...queryKeys.healthPolicies.lists(), params] as const,
    details: () => [...queryKeys.healthPolicies.all, "detail"] as const,
    detail: (id: string) => [...queryKeys.healthPolicies.details(), id] as const,
    preview: () => [...queryKeys.healthPolicies.all, "preview"] as const,
  },
  healthMetrics: {
    all: ["healthMetrics"] as const,
    list: () => [...queryKeys.healthMetrics.all, "list"] as const,
  },
  events: {
    all: ["events"] as const,
    lists: () => [...queryKeys.events.all, "list"] as const,
    list: (params: EventListParams) => [...queryKeys.events.lists(), params] as const,
  },
};
