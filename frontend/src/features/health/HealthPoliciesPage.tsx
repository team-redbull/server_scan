import { useState } from "react";
import { Link } from "react-router";

import { ApiError } from "@/api/client";
import { Badge } from "@/components/Badge";
import { HealthBadge } from "@/components/HealthBadge";
import { useDeleteHealthPolicyMutation, useHealthPoliciesQuery, useUpdateHealthPolicyMutation } from "@/features/health/hooks";
import type { HealthPolicyResponse } from "@/types/health";

export function HealthPoliciesPage() {
  const [enabledFilter, setEnabledFilter] = useState<"" | "true" | "false">("");

  const { data, isPending, isError, error } = useHealthPoliciesQuery(
    enabledFilter === "" ? {} : { enabled: enabledFilter === "true" },
  );
  const toggleMutation = useUpdateHealthPolicyMutation();
  const deleteMutation = useDeleteHealthPolicyMutation();

  const policies = data?.items ?? [];

  function handleToggle(policy: HealthPolicyResponse) {
    toggleMutation.mutate({ id: policy.id, body: { enabled: !policy.enabled } });
  }

  function handleDelete(policy: HealthPolicyResponse) {
    if (!window.confirm(`Delete health policy "${policy.name}"? This cannot be undone.`)) {
      return;
    }
    deleteMutation.mutate(policy.id);
  }

  return (
    <main className="mx-auto max-w-7xl p-8">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold">Health Policies</h1>
          <p className="mt-1 text-sm text-gray-500">
            Rules that assign per-category and overall health severity to servers.
          </p>
        </div>
        <Link
          to="/health-policies/new"
          className="rounded bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-700"
        >
          New Policy
        </Link>
      </div>

      <form
        className="mt-6 flex flex-wrap items-end gap-3"
        onSubmit={(e) => {
          e.preventDefault();
        }}
      >
        <label className="flex flex-col text-xs font-medium text-gray-500">
          Status
          <select
            value={enabledFilter}
            onChange={(e) => {
              setEnabledFilter(e.target.value as "" | "true" | "false");
            }}
            className="mt-1 rounded border border-gray-300 px-2 py-1 text-sm dark:border-gray-600 dark:bg-gray-900"
          >
            <option value="">All</option>
            <option value="true">Enabled</option>
            <option value="false">Disabled</option>
          </select>
        </label>
      </form>

      <div className="mt-4">
        {isPending && <p className="text-gray-500">Loading…</p>}

        {isError && (
          <p className="rounded border border-red-300 bg-red-50 p-3 text-red-700 dark:border-red-800 dark:bg-red-950 dark:text-red-300">
            {error instanceof ApiError
              ? error.problem.detail
              : error instanceof Error
                ? error.message
                : "Failed to load health policies."}
          </p>
        )}

        {!isPending && !isError && (
          <div className="overflow-x-auto rounded-lg border border-gray-200 dark:border-gray-700">
            <table className="min-w-full divide-y divide-gray-200 text-sm dark:divide-gray-700">
              <thead className="bg-gray-50 dark:bg-gray-800/50">
                <tr>
                  {["Name", "Category", "Severity", "policy_key", "Mode", "Status", "Toggle", "Delete"].map(
                    (h, i) => (
                      <th
                        key={h}
                        scope="col"
                        className="px-3 py-2 text-left font-medium text-gray-500 dark:text-gray-400"
                      >
                        {/* The last two columns hold action buttons, not a
                         * labeled value — the header text exists only for a
                         * unique React key and screen-reader context, not
                         * sighted display. */}
                        {i < 6 ? h : <span className="sr-only">{h}</span>}
                      </th>
                    ),
                  )}
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100 dark:divide-gray-800">
                {policies.map((policy) => (
                  <tr key={policy.id}>
                    <td className="px-3 py-2">
                      <Link
                        to={`/health-policies/${policy.id}/edit`}
                        className="font-medium text-blue-600 hover:underline dark:text-blue-400"
                      >
                        {policy.name}
                      </Link>
                      {policy.system && (
                        <span className="ml-2" title="System policy — locked except for enable/disable">
                          <Badge>system</Badge>
                        </span>
                      )}
                    </td>
                    <td className="px-3 py-2">{policy.category}</td>
                    <td className="px-3 py-2">
                      <HealthBadge severity={policy.severity} />
                    </td>
                    <td className="px-3 py-2 font-mono text-xs text-gray-500">{policy.policy_key}</td>
                    <td className="px-3 py-2">{policy.mode}</td>
                    <td className="px-3 py-2">
                      {policy.enabled ? <Badge>enabled</Badge> : <Badge tone="warning">disabled</Badge>}
                    </td>
                    <td className="px-3 py-2">
                      <button
                        type="button"
                        onClick={() => {
                          handleToggle(policy);
                        }}
                        disabled={toggleMutation.isPending}
                        className="rounded border border-gray-300 px-2 py-1 text-xs disabled:cursor-not-allowed disabled:opacity-40 dark:border-gray-600"
                      >
                        {policy.enabled ? "Disable" : "Enable"}
                      </button>
                    </td>
                    <td className="px-3 py-2">
                      {!policy.system && (
                        <button
                          type="button"
                          onClick={() => {
                            handleDelete(policy);
                          }}
                          disabled={deleteMutation.isPending}
                          className="rounded border border-red-300 px-2 py-1 text-xs text-red-700 disabled:cursor-not-allowed disabled:opacity-40 dark:border-red-800 dark:text-red-400"
                        >
                          Delete
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
                {policies.length === 0 && (
                  <tr>
                    <td colSpan={8} className="px-3 py-6 text-center text-gray-500">
                      No health policies match the current filter.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </main>
  );
}
