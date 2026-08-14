import type { HealthSeverity, MaintenanceState } from "@/types/server";

/**
 * The one column an operator actually scans for. It merges two facts that
 * were previously separate columns — health severity and "is this box in
 * maintenance" — because they answer the same question ("does this need
 * me?") and reading two columns to answer one question is what made the
 * old table slow to scan.
 *
 * Maintenance deliberately WINS over severity rather than rendering
 * alongside it: a critical alert on a server someone is actively working
 * on is expected, and showing it as CRITICAL trains people to ignore red.
 * The underlying severity is still on the detail page; this cell answers
 * "should I act", not "what is every fact about this row".
 *
 * Colorblind safety: every state carries a distinct GLYPH and a distinct
 * WORD, so color is a third, redundant signal rather than the only one.
 * Roughly 1 in 12 men cannot separate the red and green here, and this is
 * a table where that distinction is the entire point.
 */

type State = "MAINTENANCE" | HealthSeverity;

interface StateStyle {
  label: string;
  /** Redundant non-color signal. Geometric shapes, not colored dots —
   * shape survives both grayscale printing and color blindness. */
  glyph: string;
  className: string;
}

const STATES: Record<State, StateStyle> = {
  CRITICAL: {
    label: "Critical",
    glyph: "▲",
    className: "bg-[var(--tint-critical)] text-[var(--text-on-critical)]",
  },
  WARNING: {
    label: "Warning",
    glyph: "◆",
    className: "bg-[var(--tint-warning)] text-[var(--text-on-warning)]",
  },
  MAINTENANCE: {
    label: "Maintenance",
    glyph: "⏸",
    className: "bg-[var(--tint-maintenance)] text-[var(--text-on-maintenance)]",
  },
  INFO: {
    label: "Info",
    glyph: "●",
    className: "bg-[var(--tint-info)] text-[var(--text-on-info)]",
  },
  HEALTHY: {
    label: "Healthy",
    glyph: "●",
    className: "bg-[var(--tint-healthy)] text-[var(--text-on-healthy)]",
  },
  UNKNOWN: {
    label: "Unknown",
    glyph: "○",
    className: "bg-[var(--tint-unknown)] text-[var(--text-on-unknown)]",
  },
};

function resolveState(severity: HealthSeverity, maintenance: MaintenanceState): State {
  return maintenance.enabled ? "MAINTENANCE" : severity;
}

export function StateBadge({
  severity,
  maintenance,
}: {
  severity: HealthSeverity;
  maintenance: MaintenanceState;
}) {
  const state = resolveState(severity, maintenance);
  const style = STATES[state];

  // `title` carries the fact the merge hides — the real severity behind a
  // maintenance flag — so it is recoverable on hover without spending a
  // column on it.
  const title =
    state === "MAINTENANCE"
      ? `In maintenance${maintenance.reason ? `: ${maintenance.reason}` : ""} (health: ${severity})`
      : undefined;

  return (
    <span
      title={title}
      className={`inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-xs font-medium whitespace-nowrap ${style.className}`}
    >
      <span aria-hidden="true" className="text-[0.7em] leading-none">
        {style.glyph}
      </span>
      {style.label}
    </span>
  );
}
