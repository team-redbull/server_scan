import type { OpenShiftState } from "@/types/server";

/** Whether a server is in use, as a coloured pill.
 *
 * **The palette is deliberately inverted against every other badge in the
 * app: red means the server is in use, green means it is free to take.**
 * This is a capacity view, not a health view, and the two sit in the same
 * inventory row — a red Installation next to a red State means "in use"
 * and "unhealthy", which are unrelated facts.
 *
 * Two things follow from that, and both are load-bearing rather than
 * decoration:
 *
 * - The label is always rendered as text, never colour alone. Without it
 *   a red row reads as a broken server at a glance, and it is the
 *   opposite.
 * - No `SEVERITY_GLYPH` shape is used. Those belong to health, and
 *   borrowing one would make the two badge families indistinguishable for
 *   anyone who cannot separate the hues — which is exactly who the
 *   glyphs exist for.
 *
 * Colours come from the same tokens the health badge uses, so light and
 * dark themes stay consistent for free.
 */
const STYLES: Record<OpenShiftState, string> = {
  AVAILABLE: "bg-[var(--tint-healthy)] text-[var(--text-on-healthy)]",
  INSTALLED: "bg-[var(--tint-critical)] text-[var(--text-on-critical)]",
  INSTALLED_TO_INVENTORY: "bg-[var(--tint-info)] text-[var(--text-on-info)]",
};

/** Table-width labels. "In inventory" is the short form of "Installed to
 * inventory", which does not fit a column beside a hostname. */
const SHORT_LABELS: Record<OpenShiftState, string> = {
  AVAILABLE: "Available",
  INSTALLED: "Installed",
  INSTALLED_TO_INVENTORY: "In inventory",
};

/** Spelled out, for the detail page where there is room. */
const FULL_LABELS: Record<OpenShiftState, string> = {
  AVAILABLE: "Available",
  INSTALLED: "Installed",
  INSTALLED_TO_INVENTORY: "Installed to inventory",
};

export function InstallationBadge({
  state,
  full = false,
}: {
  state: OpenShiftState;
  full?: boolean;
}) {
  const labels = full ? FULL_LABELS : SHORT_LABELS;
  return (
    <span
      className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${STYLES[state]}`}
    >
      {labels[state]}
    </span>
  );
}
