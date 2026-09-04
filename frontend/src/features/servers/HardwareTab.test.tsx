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

describe("a GPU field the provider could not read", () => {
  /** The API serialises Python `None` as JSON `null`, never as an absent
   * key. `x !== undefined` therefore passed for a null and
   * `null.toFixed()` threw, unmounting the whole detail page — the E2E
   * suite caught it as "the Hardware tab button does not exist".
   */
  it("renders a dash instead of crashing the tab", () => {
    const hardware = ilo4Hardware();
    hardware.gpus = [
      {
        vendor: "NVIDIA",
        model: "NVIDIA A100 80GB",
        serial: null,
        memory_bytes: null,
        health: null,
        pci_address: null,
        firmware_version: null,
        memory_type: null,
        temperature_celsius: null,
        power_watts: null,
        ecc_mode_enabled: null,
        correctable_error_count: null,
        uncorrectable_error_count: null,
      },
    ];

    render(<HardwareTab hardware={hardware} unreadFields={[]} />);

    expect(screen.getByText("NVIDIA A100 80GB")).toBeInTheDocument();
    // Every unreadable figure degrades to the same dash rather than to
    // "NaN", "0" or a thrown TypeError.
    expect(screen.getAllByText("—").length).toBeGreaterThanOrEqual(6);
  });
});

describe("a drive, PSU or GPU the collector reported partially", () => {
  /** Nothing on the wire distinguishes "unread" from "zero" at field
   * level, so every one of these must degrade to the tab's dash rather
   * than to a `0 B`, a blank cell, or an unstyled badge. */
  it("dashes every unread drive field instead of stating a zero", () => {
    const hardware = ilo4Hardware();
    hardware.storage = {
      total_bytes: 0,
      drives: [
        {
          id: "d1",
          model: null,
          serial: null,
          media_type: "UNKNOWN",
          capacity_bytes: null,
          health: null,
        },
      ],
    };

    render(<HardwareTab hardware={hardware} unreadFields={[]} />);

    // Model, serial, capacity and health — four dashes, and no "0 B".
    expect(screen.getAllByText("—")).toHaveLength(4);
    expect(screen.queryByText("0 B")).not.toBeInTheDocument();
  });

  it("shows a PSU's own model, rating and state", () => {
    // These read `psu.status`/`psu.watts`, which the API has never sent —
    // every PSU rendered as the literal word "unknown".
    const hardware = ilo4Hardware();
    hardware.power = {
      psus: [
        { id: "PSU1", model: "800W Platinum", serial: "PSU123", health: "UP", capacity_watts: 800 },
        { id: "PSU2", model: null, serial: null, health: null, capacity_watts: null },
      ],
    };

    render(<HardwareTab hardware={hardware} unreadFields={[]} />);

    expect(screen.getByText(/800W Platinum — 800W/)).toBeInTheDocument();
    expect(screen.getByText(/PSU2/)).toBeInTheDocument();
    expect(screen.queryByText("unknown")).not.toBeInTheDocument();
  });

  it("shows a Cisco health word rather than an unstyled severity badge", () => {
    // `health` is UP/DOWN on Cisco and HEALTHY/CRITICAL on Redfish. The
    // severity badge has no class for "UP", so it rendered the word with
    // no colour at all.
    const hardware = ilo4Hardware();
    hardware.gpus = [
      {
        vendor: "NVIDIA",
        model: "UCSX-GPU-T4-16",
        serial: null,
        memory_bytes: null,
        health: "UP",
        pci_address: null,
        firmware_version: null,
        memory_type: null,
        ecc_mode_enabled: null,
        correctable_error_count: null,
        uncorrectable_error_count: null,
        temperature_celsius: null,
        power_watts: null,
      },
    ];

    render(<HardwareTab hardware={hardware} unreadFields={[]} />);

    // Rendered as the collector's own word, not through the severity
    // badge, whose colour table has no "UP" key.
    expect(screen.getByText(/UP/)).toBeInTheDocument();
    expect(screen.queryByText("UP", { selector: "span.rounded-full" })).not.toBeInTheDocument();
  });
});
