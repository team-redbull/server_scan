import { Link } from "react-router";

import { ApiError } from "@/api/client";
import { HealthBadge } from "@/components/HealthBadge";
import { useHealthPolicyPreviewQuery } from "@/features/health/hooks";
import type { HealthPolicyPreviewRequest } from "@/types/health";

interface PreviewPanelProps {
  /** The (already-debounced) draft preview request, or `null` when the
   * draft isn't complete enough to preview yet (no complete condition,
   * empty message template, etc). */
  request: HealthPolicyPreviewRequest | null;
}

export function PreviewPanel({ request }: PreviewPanelProps) {
  const { data, isPending, isError, error } = useHealthPolicyPreviewQuery(request);

  return (
    <section className="rounded-lg border border-gray-200 p-4 dark:border-gray-700">
      <h2 className="text-sm font-medium uppercase tracking-wide text-gray-500">Live Preview</h2>

      {request === null && (
        <p className="mt-2 text-sm text-gray-500">
          Fill in a complete condition and message template above to see a live preview of
          matching servers.
        </p>
      )}

      {request !== null && isPending && <p className="mt-2 text-sm text-gray-500">Loading preview…</p>}

      {request !== null && isError && (
        <p className="mt-2 rounded border border-red-300 bg-red-50 p-3 text-sm text-red-700 dark:border-red-800 dark:bg-red-950 dark:text-red-300">
          {error instanceof ApiError
            ? error.problem.detail
            : error instanceof Error
              ? error.message
              : "Failed to load preview."}
        </p>
      )}

      {request !== null && data && (
        <>
          <p className="mt-2 text-sm">
            <span className="font-medium">{data.matched_count}</span> server
            {data.matched_count === 1 ? "" : "s"} match
            {data.truncated && (
              <span className="ml-1 text-amber-600 dark:text-amber-400">(sample truncated)</span>
            )}
          </p>
          {data.sample.length > 0 ? (
            <ul className="mt-2 space-y-1 text-sm">
              {data.sample.map((s) => (
                <li key={s.id} className="flex items-center gap-2">
                  <Link
                    to={`/servers/${s.id}`}
                    className="text-blue-600 hover:underline dark:text-blue-400"
                  >
                    {s.name}
                  </Link>
                  <HealthBadge severity={s.would_be_severity} />
                </li>
              ))}
            </ul>
          ) : (
            <p className="mt-2 text-sm text-gray-500">No matching servers.</p>
          )}
        </>
      )}
    </section>
  );
}
