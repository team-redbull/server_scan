import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { queryKeys } from "@/api/queryKeys";
import {
  disableMaintenance,
  enableMaintenance,
  getServerFacets,
  listServers,
} from "@/api/servers";
import type { ServerListParams } from "@/api/servers";
import type { ServerListResponse } from "@/types/server";

/**
 * Server list query. `placeholderData: keepPreviousData` keeps the current
 * page's rows on screen (instead of flashing to a loading state) while the
 * next page/filter set is in flight — important for cursor pagination,
 * where a jarring blank state on every "Next" click would be worse than a
 * brief stale-data display.
 */
export function useServersQuery(params: ServerListParams) {
  return useQuery({
    queryKey: queryKeys.servers.list(params),
    queryFn: () => listServers(params),
    placeholderData: keepPreviousData,
  });
}

/**
 * Per-option counts for the current filter set.
 *
 * `keepPreviousData` for the same reason the list uses it: the numbers
 * sitting beside each filter should not blank out while the next set is in
 * flight, which would make every filter change flicker twice.
 */
export function useServerFacetsQuery(params: ServerListParams) {
  return useQuery({
    queryKey: queryKeys.servers.facets(params),
    queryFn: () => getServerFacets(params),
    placeholderData: keepPreviousData,
  });
}

/**
 * Toggle one server's maintenance mode from the inventory list.
 *
 * Row-agnostic — the server's id is a mutation *variable*, not a closure
 * over a hook argument, because a hook cannot be called per row. This is
 * the app's ONLY maintenance write path — the detail page is read-only.
 * `ticket` and `expected_end` are API-only today.
 *
 * Args:
 *   None.
 *
 * Returns:
 *   The mutation, taking `{ id, enable, reason? }` where `enable` is the
 *   state to move the server *to*.
 */
export function useToggleMaintenanceMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, enable, reason }: { id: string; enable: boolean; reason?: string }) =>
      enable ? enableMaintenance(id, reason ? { reason } : {}) : disableMaintenance(id),
    onSuccess: (server) => {
      queryClient.setQueryData(queryKeys.servers.detail(server.id), server);
      // Patch for the same frame, then refetch — under `?maintenance=true`
      // the row must LEAVE the list, which no patch can do. Safe only
      // because the server clears its own list cache first (ADR-0028).
      queryClient.setQueriesData<ServerListResponse>(
        { queryKey: queryKeys.servers.lists() },
        (page) =>
          page && {
            ...page,
            items: page.items.map((row) =>
              row.id === server.id ? { ...row, maintenance: server.maintenance } : row,
            ),
          },
      );
      void queryClient.invalidateQueries({ queryKey: queryKeys.servers.all });
      void queryClient.invalidateQueries({ queryKey: queryKeys.sites.all });
    },
  });
}
