import { describe, expect, it } from "vitest";

import { formatTimestamp } from "@/lib/datetime";

describe("formatTimestamp", () => {
  it("renders in Israel time regardless of a UTC-stored instant", () => {
    // 2026-01-15T10:00:00Z is winter (IST, UTC+2) — noon local.
    expect(formatTimestamp("2026-01-15T10:00:00Z")).toContain("12:00:00 PM");
  });

  it("carries the summer DST offset too (IDT, UTC+3)", () => {
    // 2026-07-15T10:00:00Z is summer — 1pm local, not the winter noon.
    expect(formatTimestamp("2026-07-15T10:00:00Z")).toContain("1:00:00 PM");
  });
});
