import { SEVERITY_GLYPH } from "@/components/severity";
import type { HealthSeverity, MaintenanceState } from "@/types/server";

/**
 * The one column an operator actually scans for: health severity, plus a
 * maintenance marker when the server is under active work.
 *
 * Maintenance is shown ALONGSIDE severity rather than replacing it. An
 * earlier version let maintenance win and hid the severity behind a
 * tooltip, on the theory that a critical alert on a box someone is
 * already working on trains people to ignore red. That reasoning is real,
 * but hiding the severity is the wrong fix for it: "critical, and someone
 * is on it" and "in maintenance, otherwise fine" are different situations
 * and the table should not render them identically. The maintenance chip
 * uses a hue outside the severity set, so the two vocabularies never
 * collide — the row reads as "Critical + Maint", not as a fourth severity.
 *
 * Colorblind safety: every severity carries a DISTINCT glyph and its own
 * word, so colour is a third, redundant signal. The glyphs must stay
 * mutually distinct — an earlier version gave HEALTHY and INFO the same
 * filled circle, which silently made them identical to anyone who cannot
 * separate green from blue, i.e. exactly the readers the glyph exists for.
 */

interface SeverityStyle {
  label: string;
  /** Distinct per severity — see the note above. Geometric shapes, so the
   * distinction survives greyscale as well as colour blindness. */
  glyph: string;
  className: string;
}

const SEVERITIES: Record<HealthSeverity, SeverityStyle> = {
  CRITICAL: {
    label: "Critical",
    glyph: SEVERITY_GLYPH.CRITICAL,
    className: "bg-[var(--tint-critical)] text-[var(--text-on-critical)]",
  },
  WARNING: {
    label: "Warning",
    glyph: SEVERITY_GLYPH.WARNING,
    className: "bg-[var(--tint-warning)] text-[var(--text-on-warning)]",
  },
  INFO: {
    label: "Info",
    glyph: SEVERITY_GLYPH.INFO,
    className: "bg-[var(--tint-info)] text-[var(--text-on-info)]",
  },
  HEALTHY: {
    label: "Healthy",
    glyph: SEVERITY_GLYPH.HEALTHY,
    className: "bg-[var(--tint-healthy)] text-[var(--text-on-healthy)]",
  },
  UNKNOWN: {
    label: "Unknown",
    glyph: SEVERITY_GLYPH.UNKNOWN,
    className: "bg-[var(--tint-unknown)] text-[var(--text-on-unknown)]",
  },
};

export function StateBadge({
  severity,
  maintenance,
}: {
  severity: HealthSeverity;
  maintenance: MaintenanceState;
}) {
  const style = SEVERITIES[severity];

  return (
    <span className="inline-flex items-center gap-2">
      <span
        className={`inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-xs font-medium whitespace-nowrap ${style.className}`}
      >
        <span aria-hidden="true" className="text-[0.7em] leading-none">
          {style.glyph}
        </span>
        {style.label}
      </span>
      {maintenance.enabled && (
        <span
          title={maintenance.reason ?? undefined}
          className="inline-flex items-center gap-1 rounded-full bg-[var(--tint-maintenance)] px-2 py-0.5 text-xs font-medium whitespace-nowrap text-[var(--text-on-maintenance)]"
        >
          <span aria-hidden="true" className="text-[0.7em] leading-none">
            ⏸
          </span>
          Maint
        </span>
      )}
    </span>
  );
}
