import { useQuery } from "@tanstack/react-query";

import type { ClassificationRuleListParams } from "@/api/classificationRules";
import { listClassificationRules } from "@/api/classificationRules";
import { queryKeys } from "@/api/queryKeys";

/** Read-only by design. Classification rules ship with the platform and
 * are seeded at startup, so there is no create/update/delete hook here to
 * reach for — see `@/features/rules/RulesPage`. */
export function useClassificationRulesQuery(params: ClassificationRuleListParams = {}) {
  return useQuery({
    queryKey: queryKeys.classificationRules.list(params),
    queryFn: () => listClassificationRules(params),
  });
}
