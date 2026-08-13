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
    },
  });
}

export function useDisableMaintenanceMutation(id: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => disableMaintenance(id),
    onSuccess: (server) => {
      queryClient.setQueryData(queryKeys.servers.detail(id), server);
    },
  });
}
