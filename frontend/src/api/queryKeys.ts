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
};
