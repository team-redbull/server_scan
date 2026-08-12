import type { ReactNode } from "react";

/** Generic neutral pill badge for labels that don't carry a severity color
 * (installation type, maintenance flag, etc). */
export function Badge({
  children,
  tone = "neutral",
}: {
  children: ReactNode;
  tone?: "neutral" | "warning";
}) {
  const toneClass =
    tone === "warning"
      ? "bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300"
      : "bg-gray-100 text-gray-700 dark:bg-gray-800 dark:text-gray-300";

  return (
    <span className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${toneClass}`}>
      {children}
    </span>
  );
}
