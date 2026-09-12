import { useQuery } from "@tanstack/react-query";

import { queryKeys } from "@/api/queryKeys";
import { getServer } from "@/api/servers";

export function useServerDetailQuery(id: string) {
  return useQuery({
    queryKey: queryKeys.servers.detail(id),
    queryFn: () => getServer(id),
    enabled: id.length > 0,
  });
}
