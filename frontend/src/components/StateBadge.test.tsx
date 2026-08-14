import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { StateBadge } from "@/components/StateBadge";
import type { HealthSeverity } from "@/types/server";

const SEVERITIES: HealthSeverity[] = ["HEALTHY", "INFO", "WARNING", "CRITICAL", "UNKNOWN"];

const NOT_IN_MAINTENANCE = { enabled: false };

describe("StateBadge", () => {
  it.each(SEVERITIES)("labels %s in words, not colour alone", (severity) => {
    render(<StateBadge severity={severity} maintenance={NOT_IN_MAINTENANCE} />);
    // Capitalised word form, e.g. CRITICAL -> "Critical".
    const label = severity.charAt(0) + severity.slice(1).toLowerCase();
    expect(screen.getByText(label)).toBeInTheDocument();
  });

  it("gives every severity a distinct glyph", () => {
    // The regression this exists for: HEALTHY and INFO once shared the
    // same filled circle, which made them identical to a reader who
    // cannot separate green from blue — exactly the reader the glyph is
    // there to serve. Colour must never be the only thing that differs.
    const glyphs = SEVERITIES.map((severity) => {
      const { container, unmount } = render(
        <StateBadge severity={severity} maintenance={NOT_IN_MAINTENANCE} />,
      );
      const glyph = container.querySelector('[aria-hidden="true"]')?.textContent ?? "";
      unmount();
      return glyph;
    });

    expect(new Set(glyphs).size).toBe(SEVERITIES.length);
  });

  it("shows maintenance alongside the severity, not instead of it", () => {
    // A critical server someone is actively working on is a different
    // situation from a healthy one in maintenance; the table must not
    // render them the same.
    render(<StateBadge severity="CRITICAL" maintenance={{ enabled: true }} />);
    expect(screen.getByText("Critical")).toBeInTheDocument();
    expect(screen.getByText("Maint")).toBeInTheDocument();
  });

  it("omits the maintenance chip when not in maintenance", () => {
    render(<StateBadge severity="HEALTHY" maintenance={NOT_IN_MAINTENANCE} />);
    expect(screen.queryByText("Maint")).not.toBeInTheDocument();
  });

  it("exposes the maintenance reason without spending a column on it", () => {
    render(<StateBadge severity="HEALTHY" maintenance={{ enabled: true, reason: "PSU swap" }} />);
    expect(screen.getByText("Maint")).toHaveAttribute("title", "PSU swap");
  });
});
