import { render, screen, within } from "@testing-library/react";
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

    // Storage total, storage drives and GPU are each their own block —
    // three, not two: total and drives are unread independently of each
    // other (2026-09-09), matching Memory's total being independent of
    // its own module list.
    expect(screen.getAllByText("Not reported")).toHaveLength(3);
    // The zero must not be presented as a reading anywhere.
    expect(screen.queryByText(/No storage data/)).not.toBeInTheDocument();
    expect(screen.queryByText(/0 B total/)).not.toBeInTheDocument();

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
          health_detail: null,
        },
      ],
    };
    render(<HardwareTab hardware={hardware} unreadFields={UNREAD} />);

    // Good data is never hidden — only dimmed and explained, and the
    // explanation is visible text, not just a hover-only title.
    const drive = screen.getByText("MZ7LH3T8");
    expect(drive).toBeInTheDocument();
    const wrapper = drive.closest("div.opacity-70");
    expect(wrapper).toHaveAttribute(
      "title",
      "Not confirmed by the most recent collection.",
    );
    expect(within(wrapper as HTMLElement).getAllByText("unconfirmed").length).toBeGreaterThan(0);
  });

  it("renders unchanged when nothing was unread", () => {
    render(<HardwareTab hardware={ilo4Hardware()} />);
    expect(screen.queryByText("Not reported")).not.toBeInTheDocument();
    // A confirmed-read zero total is a real reading, same as Memory's own
    // "0 B total" would be — it renders plainly rather than as
    // "No storage data.", which is reserved for the per-drive block
    // genuinely having nothing.
    expect(screen.getByText(/0 B total/)).toBeInTheDocument();
    expect(screen.getByText("No per-drive detail.")).toBeInTheDocument();
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
        health_detail: null,
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
          health_detail: null,
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
        {
          id: "PSU1",
          model: "800W Platinum",
          serial: "PSU123",
          health: "UP",
          health_detail: null,
          capacity_watts: 800,
        },
        {
          id: "PSU2",
          model: null,
          serial: null,
          health: null,
          health_detail: null,
          capacity_watts: null,
        },
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
        health_detail: null,
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

describe("Storage's total, independent of per-drive detail", () => {
  it("shows a real total even when the drive list is empty", () => {
    // The bug this fixes: the total used to live *inside* the same
    // Reported block as the drives table, so a real, nonzero total was
    // hidden behind "No storage data." whenever drives alone came back
    // empty — exactly the OME/OpenManage shape where a bulk total is
    // known before any per-drive detail is.
    const hardware = ilo4Hardware();
    hardware.storage = { total_bytes: 4 * 1024 ** 4, drives: [] };

    render(<HardwareTab hardware={hardware} unreadFields={[]} />);

    expect(screen.getByText(/4.0 TB total/)).toBeInTheDocument();
    expect(screen.getByText("No per-drive detail.")).toBeInTheDocument();
    expect(screen.queryByText(/No storage data/)).not.toBeInTheDocument();
  });

  it("still shows the total when only the drive list is unread", () => {
    const hardware = ilo4Hardware();
    hardware.storage = { total_bytes: 4 * 1024 ** 4, drives: [] };

    render(
      <HardwareTab hardware={hardware} unreadFields={["hardware.storage.drives"]} />,
    );

    expect(screen.getByText(/4.0 TB total/)).toBeInTheDocument();
    expect(screen.getByText("Not reported")).toBeInTheDocument();
  });
});

describe("a component's health reason, alongside its severity", () => {
  it("shows a drive's raw health_detail next to its health badge", () => {
    const hardware = ilo4Hardware();
    hardware.storage = {
      total_bytes: 4 * 1024 ** 4,
      drives: [
        {
          id: "d1",
          model: "MZ7LH3T8",
          serial: "DRIVE-1",
          media_type: "SSD",
          capacity_bytes: 4 * 1024 ** 4,
          health: "CRITICAL",
          health_detail: "self-test-failed",
        },
      ],
    };

    render(<HardwareTab hardware={hardware} unreadFields={[]} />);

    expect(screen.getByText("(self-test-failed)")).toBeInTheDocument();
  });

  it("shows nothing extra when there is no reason to show", () => {
    const hardware = ilo4Hardware();
    hardware.storage = {
      total_bytes: 4 * 1024 ** 4,
      drives: [
        {
          id: "d1",
          model: "MZ7LH3T8",
          serial: "DRIVE-1",
          media_type: "SSD",
          capacity_bytes: 4 * 1024 ** 4,
          health: "HEALTHY",
          health_detail: null,
        },
      ],
    };

    render(<HardwareTab hardware={hardware} unreadFields={[]} />);

    // Scoped to the drive's own row: Memory's own "(0 modules)" text
    // elsewhere on the page would otherwise false-match a broader query.
    const row = screen.getByText("MZ7LH3T8").closest("tr") as HTMLElement;
    expect(within(row).queryByText(/\(.*\)/)).not.toBeInTheDocument();
  });

  it("shows a PSU's and a GPU's own reason the same way", () => {
    const hardware = ilo4Hardware();
    hardware.power = {
      psus: [
        {
          id: "PSU1",
          model: "800W Platinum",
          serial: "PSU123",
          health: "DOWN",
          health_detail: "inoperable",
          capacity_watts: 800,
        },
      ],
    };
    hardware.gpus = [
      {
        vendor: "NVIDIA",
        model: "A100",
        serial: null,
        memory_bytes: null,
        health: "CRITICAL",
        health_detail: "Critical",
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

    expect(screen.getByText("(inoperable)")).toBeInTheDocument();
    expect(screen.getByText("(Critical)")).toBeInTheDocument();
  });
});
