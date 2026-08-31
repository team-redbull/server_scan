import { useQuery } from "@tanstack/react-query";

import { queryKeys } from "@/api/queryKeys";
import { listSites } from "@/api/sites";

/**
 * The site list, shared by every page that needs to name or offer a site.
 *
 * One query key means the overview, the inventory filter and both policy
 * editors read the same cached response — so the set of sites they show
 * can never disagree, and adding or renaming one in the backend enum
 * needs no frontend change at all.
 */
export function useSitesQuery() {
  return useQuery({
    queryKey: queryKeys.sites.list(),
    queryFn: () => listSites(),
  });
}
