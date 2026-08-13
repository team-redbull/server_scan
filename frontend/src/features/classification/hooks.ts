import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import type { ClassificationRuleListParams } from "@/api/classificationRules";
import {
  createClassificationRule,
  deleteClassificationRule,
  getClassificationRule,
  listClassificationRules,
  previewClassificationRule,
  updateClassificationRule,
} from "@/api/classificationRules";
import { queryKeys } from "@/api/queryKeys";
import type {
  ClassificationPreviewRequest,
  ClassificationRuleCreate,
  ClassificationRuleUpdate,
} from "@/types/classification";

export function useClassificationRulesQuery(params: ClassificationRuleListParams = {}) {
  return useQuery({
    queryKey: queryKeys.classificationRules.list(params),
    queryFn: () => listClassificationRules(params),
  });
}

export function useClassificationRuleQuery(id: string) {
  return useQuery({
    queryKey: queryKeys.classificationRules.detail(id),
    queryFn: () => getClassificationRule(id),
    enabled: id.length > 0,
  });
}

export function useCreateClassificationRuleMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: ClassificationRuleCreate) => createClassificationRule(body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.classificationRules.lists() });
    },
  });
}

export function useUpdateClassificationRuleMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: { id: string; body: ClassificationRuleUpdate }) =>
      updateClassificationRule(id, body),
    onSuccess: (_data, variables) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.classificationRules.lists() });
      void queryClient.invalidateQueries({
        queryKey: queryKeys.classificationRules.detail(variables.id),
      });
    },
  });
}

export function useDeleteClassificationRuleMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => deleteClassificationRule(id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.classificationRules.lists() });
    },
  });
}

/** Live preview query. `request === null` means "not enough info to
 * preview yet" (field/pattern not both filled in) — the caller is
 * responsible for that gate, this hook just respects it via `enabled`. */
export function useClassificationPreviewQuery(request: ClassificationPreviewRequest | null) {
  return useQuery({
    queryKey: [...queryKeys.classificationRules.preview(), request],
    queryFn: () => previewClassificationRule(request as ClassificationPreviewRequest),
    enabled: request !== null,
    placeholderData: (previous) => previous,
  });
}
