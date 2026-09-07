import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ConnectivityTab } from "@/features/servers/ConnectivityTab";
import type { ConnectivityAttachment, ConnectivityDetail } from "@/types/server";

function makeAttachment(overrides: Partial<ConnectivityAttachment> = {}): ConnectivityAttachment {
  return {
    type: "FABRIC_INTERCONNECT",
    provider: "UCS_MANAGER",
    fabric: "A",
    fabric_name: "FI-A-TLV-01",
    fabric_id: "fi-01",
    fabric_model: "UCS-FI-6454",
    fabric_serial: "FCH123",
    server_interface: "vic1",
    server_port: "1",
    fabric_port: "Ethernet1/17",
    admin_state: "ENABLED",
    oper_state: "UP",
    speed_mbps: 25000,
    last_seen: "2026-08-12T10:00:00Z",
    interface_kind: "PHYSICAL",
    ...overrides,
  };
}

function makeDetail(attachments: ConnectivityAttachment[]): ConnectivityDetail {
  const up = attachments.filter((a) => a.oper_state === "UP").length;
  return {
    attachments,
    facts: {
      fabric_paths_total: attachments.length,
      fabric_paths_up: up,
      fabric_paths_down: attachments.length - up,
      fabrics_present: [...new Set(attachments.map((a) => a.fabric).filter((f): f is string => f !== null))],
    },
  };
}

describe("ConnectivityTab", () => {
  it("shows an empty state when there are zero attachments", () => {
    render(<ConnectivityTab connectivity={makeDetail([])} />);
    expect(screen.getByText("No connectivity data.")).toBeInTheDocument();
    expect(screen.queryAllByTestId("fabric-group")).toHaveLength(0);
  });

  it("shows an empty state when connectivity is undefined", () => {
    render(<ConnectivityTab connectivity={undefined} />);
    expect(screen.getByText("No connectivity data.")).toBeInTheDocument();
  });

  it("renders one group for a single attachment", () => {
    render(<ConnectivityTab connectivity={makeDetail([makeAttachment({ fabric: "A" })])} />);
    expect(screen.getAllByTestId("fabric-group")).toHaveLength(1);
    expect(screen.getByText("Fabric A")).toBeInTheDocument();
  });

  it("renders two groups for a dual-fabric attachment pair", () => {
    render(
      <ConnectivityTab
        connectivity={makeDetail([
          makeAttachment({ fabric: "A", fabric_id: "fi-01" }),
          makeAttachment({ fabric: "B", fabric_id: "fi-02", oper_state: "DOWN" }),
        ])}
      />,
    );
    expect(screen.getAllByTestId("fabric-group")).toHaveLength(2);
    expect(screen.getByText("Fabric A")).toBeInTheDocument();
    expect(screen.getByText("Fabric B")).toBeInTheDocument();
  });

  it("groups four attachments across three distinct fabrics (A/A/B/C)", () => {
    render(
      <ConnectivityTab
        connectivity={makeDetail([
          makeAttachment({ fabric: "A", fabric_id: "fi-01", server_port: "1" }),
          makeAttachment({ fabric: "A", fabric_id: "fi-01", server_port: "2" }),
          makeAttachment({ fabric: "B", fabric_id: "fi-02", server_port: "1" }),
          makeAttachment({ fabric: "C", fabric_id: "fi-03", server_port: "1" }),
        ])}
      />,
    );
    const groups = screen.getAllByTestId("fabric-group");
    expect(groups).toHaveLength(3);
    expect(screen.getByText("Fabric A")).toBeInTheDocument();
    expect(screen.getByText("Fabric B")).toBeInTheDocument();
    expect(screen.getByText("Fabric C")).toBeInTheDocument();
  });

  it("groups null-fabric attachments under a trailing 'Other' section", () => {
    render(
      <ConnectivityTab
        connectivity={makeDetail([
          makeAttachment({ fabric: "A", fabric_id: "fi-01" }),
          makeAttachment({ fabric: null, fabric_id: "misc-1", fabric_name: "Misc link" }),
        ])}
      />,
    );
    expect(screen.getAllByTestId("fabric-group")).toHaveLength(2);
    expect(screen.getByText("Fabric A")).toBeInTheDocument();
    expect(screen.getByText("Fabric Other")).toBeInTheDocument();
  });

  it("shows only the physical uplink, not the vNICs riding the same fabric", () => {
    render(
      <ConnectivityTab
        connectivity={makeDetail([
          makeAttachment({ fabric: "A", server_interface: "eth0", interface_kind: "PHYSICAL" }),
          makeAttachment({ fabric: "A", server_interface: "vnic0", interface_kind: "VNIC" }),
          makeAttachment({ fabric: "A", server_interface: "vnic1", interface_kind: "VNIC" }),
        ])}
      />,
    );
    expect(screen.getAllByTestId("fabric-group")).toHaveLength(1);
    expect(screen.getByText("eth0")).toBeInTheDocument();
    expect(screen.queryByText("vnic0")).not.toBeInTheDocument();
    expect(screen.queryByText("vnic1")).not.toBeInTheDocument();
  });

  it("shows a distinct empty state for a server with only vNIC attachments", () => {
    render(
      <ConnectivityTab
        connectivity={makeDetail([makeAttachment({ fabric: "A", interface_kind: "VNIC" })])}
      />,
    );
    expect(screen.getByText("No physical fabric connections reported.")).toBeInTheDocument();
    expect(screen.queryAllByTestId("fabric-group")).toHaveLength(0);
  });
});
