import type { HealthPolicyResponse, PolicyScope } from "@/types/health";

function scopeSummary(scope: PolicyScope): string {
  const parts: string[] = [];
  if (scope.vendor) parts.push(`vendor=${scope.vendor}`);
  if (scope.manager_type) parts.push(`manager=${scope.manager_type}`);
  if (scope.site_id) parts.push(`site=${scope.site_id}`);
  return parts.length > 0 ? parts.join(", ") : "(unscoped)";
}

interface ShadowPanelProps {
  /** All known policies (from `GET /health-policies`), used purely as a
   * client-side derivation — no dedicated backend endpoint for this. */
  policies: HealthPolicyResponse[];
  policyKey: string;
  /** The policy currently being edited, excluded from its own sibling
   * list. Omitted (or empty) on create, where there's no "self" yet. */
  currentId?: string;
}

/**
 * Shadow/override awareness: the platform's headline feature (a
 * site-scoped override replacing a global default) surfaces here as a
 * plain read of policies sharing this draft's `policy_key` — evaluation
 * picks the highest-priority, most-specific match among same-`policy_key`
 * policies and the rest are shadowed. This panel doesn't reimplement that
 * resolution logic, it only lists who's competing so the author isn't
 * flying blind.
 */
export function ShadowPanel({ policies, policyKey, currentId }: ShadowPanelProps) {
  if (!policyKey) {
    return null;
  }
  const siblings = policies.filter((p) => p.policy_key === policyKey && p.id !== currentId);
  if (siblings.length === 0) {
    return null;
  }

  return (
    <section
      data-testid="shadow-panel"
      className="rounded-lg border border-amber-300 bg-amber-50 p-4 text-sm dark:border-amber-800 dark:bg-amber-950"
    >
      <p className="font-medium text-amber-800 dark:text-amber-300">
        This policy shares its policy_key with {siblings.length} other polic
        {siblings.length === 1 ? "y" : "ies"}.
      </p>
      <p className="mt-1 text-amber-700 dark:text-amber-400">
        Only the highest-priority, most-specific match for a given server actually evaluates —
        the others are shadowed.
      </p>
      <ul className="mt-2 space-y-1">
        {siblings.map((sibling) => (
          <li key={sibling.id} className="text-amber-800 dark:text-amber-300">
            <span className="font-medium">{sibling.name}</span> — {sibling.source} (priority{" "}
            {sibling.priority}), scope: {scopeSummary(sibling.scope)}
          </li>
        ))}
      </ul>
    </section>
  );
}
