import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { HardwareTab } from "@/features/servers/HardwareTab";
import type { HardwareInfo } from "@/types/server";

/** An HPE Gen9: OneView refuses every subresource call against its iLO 4,
 * so storage and GPUs arrive as the model's zero rather than a reading. */
function ilo4Hardware(): HardwareInfo {
  return {
    cpu: { sockets: 2, cores: 32, threads: 64, model: "Xeon E5-2690 v4" },
    memory: { total_bytes: 256 * 1024 ** 3, modules: [] },
    storage: { total_bytes: 0, drives: [] },
    gpus: [],
    power: { psus: [] },
  };
}

const UNREAD = ["hardware.storage.total_bytes", "hardware.storage.drives", "hardware.gpus"];

describe("HardwareTab unread fields", () => {
  it("says 'Not reported' rather than showing the zero a collector never read", () => {
    render(<HardwareTab hardware={ilo4Hardware()} unreadFields={UNREAD} />);

    // Storage and GPU both unread — two blocks, not one.
    expect(screen.getAllByText("Not reported")).toHaveLength(2);
    // The zero must not be presented as a reading anywhere.
    expect(screen.queryByText(/No storage data/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Total: 0 B/)).not.toBeInTheDocument();

    // Fields the same run *did* read stay untouched.
    expect(screen.getByText("Xeon E5-2690 v4")).toBeInTheDocument();
    expect(screen.getByText(/256.0 GB total/)).toBeInTheDocument();
  });

  it("keeps a carried-forward value visible, marked as unconfirmed", () => {
    const hardware = ilo4Hardware();
    hardware.storage = {
      total_bytes: 2 * 1024 ** 4,
      drives: [
        {
          id: "d1",
          model: "MZ7LH3T8",
          serial: "DRIVE-1",
          media_type: "SSD",
          capacity_bytes: 2 * 1024 ** 4,
          health: "HEALTHY",
        },
      ],
    };
    render(<HardwareTab hardware={hardware} unreadFields={UNREAD} />);

    // Good data is never hidden — only dimmed and explained.
    const drive = screen.getByText("MZ7LH3T8");
    expect(drive).toBeInTheDocument();
    expect(drive.closest("div.opacity-50")).toHaveAttribute(
      "title",
      "Not confirmed by the most recent collection.",
    );
  });

  it("renders unchanged when nothing was unread", () => {
    render(<HardwareTab hardware={ilo4Hardware()} />);
    expect(screen.queryByText("Not reported")).not.toBeInTheDocument();
    expect(screen.getByText("No storage data.")).toBeInTheDocument();
    expect(screen.getByText("No GPUs.")).toBeInTheDocument();
  });
});
