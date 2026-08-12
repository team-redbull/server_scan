import type { HealthSeverity } from "@/types/server";

// Keyed by the full `HealthSeverity` union (not a generic index signature),
// so this stays exhaustive under `noUncheckedIndexedAccess` without an
// `undefined` branch on lookup.
const SEVERITY_STYLES: Record<HealthSeverity, string> = {
  HEALTHY: "bg-green-100 text-green-800 dark:bg-green-900/40 dark:text-green-300",
  INFO: "bg-blue-100 text-blue-800 dark:bg-blue-900/40 dark:text-blue-300",
  WARNING: "bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300",
  CRITICAL: "bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-300",
  UNKNOWN: "bg-gray-100 text-gray-700 dark:bg-gray-800 dark:text-gray-300",
};

export function HealthBadge({ severity }: { severity: HealthSeverity }) {
  return (
    <span
      className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${SEVERITY_STYLES[severity]}`}
    >
      {severity}
    </span>
  );
}
