import { keepPreviousData, useQuery } from "@tanstack/react-query";

import { queryKeys } from "@/api/queryKeys";
import { getServerFacets, listServers } from "@/api/servers";
import type { ServerListParams } from "@/api/servers";

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
