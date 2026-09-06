import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { queryKeys } from "@/api/queryKeys";
import type { MaintenanceEnableRequest } from "@/api/servers";
import { disableMaintenance, enableMaintenance, getServer } from "@/api/servers";

export function useServerDetailQuery(id: string) {
  return useQuery({
    queryKey: queryKeys.servers.detail(id),
    queryFn: () => getServer(id),
    enabled: id.length > 0,
  });
}

export function useEnableMaintenanceMutation(id: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: MaintenanceEnableRequest) => enableMaintenance(id, body),
    onSuccess: (server) => {
      queryClient.setQueryData(queryKeys.servers.detail(id), server);
      // Invalidate the whole `servers` branch — list AND facets, which is
      // a sibling key under `servers`, not a child of `lists()` — plus
      // every site card's `in_maintenance` count. This is the app's only
      // write path, so nothing else invalidates any of them.
      void queryClient.invalidateQueries({ queryKey: queryKeys.servers.all });
      void queryClient.invalidateQueries({ queryKey: queryKeys.sites.all });
    },
  });
}

export function useDisableMaintenanceMutation(id: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => disableMaintenance(id),
    onSuccess: (server) => {
      queryClient.setQueryData(queryKeys.servers.detail(id), server);
      void queryClient.invalidateQueries({ queryKey: queryKeys.servers.lists() });
      void queryClient.invalidateQueries({ queryKey: queryKeys.sites.all });
    },
  });
}
