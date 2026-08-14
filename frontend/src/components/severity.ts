import type { HealthSeverity } from "@/types/server";

/**
 * The one place a severity's shape is decided.
 *
 * Anything that shows a severity — the inventory table's State cell, the
 * site cards' critical/warning counts — reads its glyph from here, so a
 * shape can never come to mean one thing on one screen and something
 * else on another. They did once: the table drew critical as a diamond
 * while the site cards drew it as a triangle.
 *
 * The glyphs must also stay mutually distinct. Colour is the *third*
 * signal after shape and word, and an earlier version gave HEALTHY and
 * INFO the same filled circle — which made them identical to a reader who
 * cannot separate green from blue, exactly the reader the glyph exists
 * for. `StateBadge.test.tsx` asserts they stay distinct.
 *
 * Lives in its own module rather than beside the component because a file
 * that exports both a component and a constant breaks React Fast Refresh:
 * the dev server can no longer hot-swap the component alone and falls
 * back to a full reload.
 */
export const SEVERITY_GLYPH: Record<HealthSeverity, string> = {
  CRITICAL: "◆", // filled diamond
  WARNING: "▲", // filled triangle
  INFO: "■", // filled square
  HEALTHY: "●", // filled circle
  UNKNOWN: "○", // hollow circle — no filled reading
};
