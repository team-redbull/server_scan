import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import type { HealthPolicyListParams } from "@/api/healthPolicies";
import {
  createHealthPolicy,
  deleteHealthPolicy,
  getHealthPolicy,
  listHealthPolicies,
  previewHealthPolicy,
  updateHealthPolicy,
} from "@/api/healthPolicies";
import { listHealthMetrics } from "@/api/healthMetrics";
import { queryKeys } from "@/api/queryKeys";
import type {
  HealthPolicyCreate,
  HealthPolicyPreviewRequest,
  HealthPolicyUpdate,
} from "@/types/health";

export function useHealthPoliciesQuery(params: HealthPolicyListParams = {}) {
  return useQuery({
    queryKey: queryKeys.healthPolicies.list(params),
    queryFn: () => listHealthPolicies(params),
  });
}

export function useHealthPolicyQuery(id: string) {
  return useQuery({
    queryKey: queryKeys.healthPolicies.detail(id),
    queryFn: () => getHealthPolicy(id),
    enabled: id.length > 0,
  });
}

export function useHealthMetricsQuery() {
  return useQuery({
    queryKey: queryKeys.healthMetrics.list(),
    queryFn: () => listHealthMetrics(),
    staleTime: Infinity,
  });
}

export function useCreateHealthPolicyMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: HealthPolicyCreate) => createHealthPolicy(body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.healthPolicies.lists() });
    },
  });
}

export function useUpdateHealthPolicyMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: { id: string; body: HealthPolicyUpdate }) =>
      updateHealthPolicy(id, body),
    onSuccess: (_data, variables) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.healthPolicies.lists() });
      void queryClient.invalidateQueries({ queryKey: queryKeys.healthPolicies.detail(variables.id) });
    },
  });
}

export function useDeleteHealthPolicyMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => deleteHealthPolicy(id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.healthPolicies.lists() });
    },
  });
}

/** Live preview query. `request === null` means "not enough info to
 * preview yet" — the caller gates that (condition needs at least one
 * complete leaf, message_template non-empty, etc). */
export function useHealthPolicyPreviewQuery(request: HealthPolicyPreviewRequest | null) {
  return useQuery({
    queryKey: [...queryKeys.healthPolicies.preview(), request],
    queryFn: () => previewHealthPolicy(request as HealthPolicyPreviewRequest),
    enabled: request !== null,
    placeholderData: (previous) => previous,
  });
}
