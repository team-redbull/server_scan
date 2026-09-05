import { ApiError } from "@/api/client";
import { Badge } from "@/components/Badge";
import { HealthBadge } from "@/components/HealthBadge";
import { useClassificationRulesQuery } from "@/features/classification/hooks";
import { useHealthPoliciesQuery } from "@/features/health/hooks";
import type { ClassificationRuleResponse } from "@/types/classification";
import type { HealthPolicyResponse } from "@/types/health";

/** The scope a rule or policy is narrowed to, as one readable string.
 *
 * `(unscoped)` rather than an empty cell: a rule that applies to the whole
 * fleet is a deliberate choice, and a blank reads as missing data. */
function scopeSummary(scope: {
  vendor?: string | null;
  manager_type?: string | null;
  site_id?: string | null;
}): string {
  const parts: string[] = [];
  if (scope.vendor) parts.push(`vendor=${scope.vendor}`);
  if (scope.manager_type) parts.push(`manager=${scope.manager_type}`);
  if (scope.site_id) parts.push(`site=${scope.site_id}`);
  return parts.length > 0 ? parts.join(", ") : "(unscoped)";
}

function errorText(error: unknown, fallback: string): string {
  if (error instanceof ApiError) return error.problem.detail;
  if (error instanceof Error) return error.message;
  return fallback;
}

export function RulesPage() {
  const rulesQuery = useClassificationRulesQuery();
  const policiesQuery = useHealthPoliciesQuery();

  const rules = rulesQuery.data?.items ?? [];
  const policies = policiesQuery.data?.items ?? [];

  return (
    <main className="mx-auto max-w-7xl p-8">
      <h1 className="text-2xl font-semibold">Rules &amp; Policies</h1>
      {/* Says the constraint plainly rather than leaving someone to
       * discover it by looking for an edit button that is not there. The
       * point is not that editing is unimplemented — it is that every
       * deployment of this platform classifies and scores identically,
       * which stops being true the moment one site's operator adds a rule
       * the others do not have. */}
      <p className="mt-1 max-w-3xl text-sm text-gray-500">
        What this platform ships with, and every deployment gets the same
        set. They are defined in code and seeded at startup, not created
        here — a rule that exists in one estate and not another makes two
        installations that look identical classify the same server
        differently.
      </p>

      <section className="mt-8">
        <h2 className="text-lg font-semibold">Classification rules</h2>
        <p className="mt-1 text-sm text-gray-500">
          Assign an installation type (HOSTED_CLUSTER / UPI / UNCLASSIFIED)
          from a server&apos;s own fields.
        </p>

        {rulesQuery.isPending && <p className="mt-3 text-gray-500">Loading…</p>}
        {rulesQuery.isError && (
          <p className="mt-3 rounded border border-red-300 bg-red-50 p-3 text-red-700 dark:border-red-800 dark:bg-red-950 dark:text-red-300">
            {errorText(rulesQuery.error, "Failed to load classification rules.")}
          </p>
        )}

        {!rulesQuery.isPending && !rulesQuery.isError && (
          <div className="mt-3 overflow-x-auto rounded-lg border border-gray-200 dark:border-gray-700">
            <table className="min-w-full divide-y divide-gray-200 text-sm dark:divide-gray-700">
              <thead className="bg-gray-50 dark:bg-gray-800/50">
                <tr>
                  {["Name", "Type", "Source", "Priority", "Scope", "Status"].map((h) => (
                    <th
                      key={h}
                      scope="col"
                      className="px-3 py-2 text-left font-medium text-gray-500 dark:text-gray-400"
                    >
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100 dark:divide-gray-800">
                {rules.map((rule: ClassificationRuleResponse) => (
                  <tr key={rule.id}>
                    <td className="px-3 py-2 font-medium">{rule.name}</td>
                    <td className="px-3 py-2">
                      <Badge>{rule.installation_type}</Badge>
                    </td>
                    <td className="px-3 py-2">{rule.source}</td>
                    <td className="px-3 py-2">{rule.priority}</td>
                    <td className="px-3 py-2 text-gray-500">{scopeSummary(rule.scope)}</td>
                    <td className="px-3 py-2">
                      {rule.enabled ? (
                        <Badge>enabled</Badge>
                      ) : (
                        <Badge tone="warning">disabled</Badge>
                      )}
                    </td>
                  </tr>
                ))}
                {rules.length === 0 && (
                  <tr>
                    <td colSpan={6} className="px-3 py-6 text-center text-gray-500">
                      No classification rules are configured.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="mt-10">
        <h2 className="text-lg font-semibold">Health policies</h2>
        <p className="mt-1 text-sm text-gray-500">
          Assign per-category and overall health severity. Within one{" "}
          <code className="font-mono text-xs">policy_key</code> the
          highest-priority enabled policy wins and the rest are shadowed.
        </p>

        {policiesQuery.isPending && <p className="mt-3 text-gray-500">Loading…</p>}
        {policiesQuery.isError && (
          <p className="mt-3 rounded border border-red-300 bg-red-50 p-3 text-red-700 dark:border-red-800 dark:bg-red-950 dark:text-red-300">
            {errorText(policiesQuery.error, "Failed to load health policies.")}
          </p>
        )}

        {!policiesQuery.isPending && !policiesQuery.isError && (
          <div className="mt-3 overflow-x-auto rounded-lg border border-gray-200 dark:border-gray-700">
            <table className="min-w-full divide-y divide-gray-200 text-sm dark:divide-gray-700">
              <thead className="bg-gray-50 dark:bg-gray-800/50">
                <tr>
                  {["Name", "Category", "Severity", "policy_key", "Mode", "Scope", "Status"].map(
                    (h) => (
                      <th
                        key={h}
                        scope="col"
                        className="px-3 py-2 text-left font-medium text-gray-500 dark:text-gray-400"
                      >
                        {h}
                      </th>
                    ),
                  )}
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100 dark:divide-gray-800">
                {policies.map((policy: HealthPolicyResponse) => (
                  <tr key={policy.id}>
                    <td className="px-3 py-2 font-medium">{policy.name}</td>
                    <td className="px-3 py-2">{policy.category}</td>
                    <td className="px-3 py-2">
                      <HealthBadge severity={policy.severity} />
                    </td>
                    <td className="px-3 py-2 font-mono text-xs text-gray-500">
                      {policy.policy_key}
                    </td>
                    <td className="px-3 py-2">{policy.mode}</td>
                    <td className="px-3 py-2 text-gray-500">{scopeSummary(policy.scope)}</td>
                    <td className="px-3 py-2">
                      {policy.enabled ? (
                        <Badge>enabled</Badge>
                      ) : (
                        <Badge tone="warning">disabled</Badge>
                      )}
                    </td>
                  </tr>
                ))}
                {policies.length === 0 && (
                  <tr>
                    <td colSpan={7} className="px-3 py-6 text-center text-gray-500">
                      No health policies are configured.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </main>
  );
}
