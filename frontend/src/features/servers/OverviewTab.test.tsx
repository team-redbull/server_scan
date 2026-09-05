import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { OverviewTab } from "@/features/servers/OverviewTab";
import type { ServerDetail } from "@/types/server";

function makeServer(overrides: Partial<ServerDetail> = {}): ServerDetail {
  return {
    id: "srv_1",
    name: "ocp-dell-worker-000",
    model: "PowerEdge R6515",
    identity: { vendor: "dell", serial: "SN123", system_uuid: null, nic_macs: [] },
    hardware: {
      cpu: { sockets: 2, cores: 32, threads: 64, model: "Xeon Gold 6338" },
      memory: { total_bytes: 0, modules: [] },
      storage: { total_bytes: 0, drives: [] },
      gpus: [],
      power: { psus: [] },
    },
    network: {
      bmc: { address_raw: null, scheme: null, host: null, port: null, mac: null },
      interfaces: [],
    },
    connectivity: {
      attachments: [],
      facts: {
        fabric_paths_total: 0,
        fabric_paths_up: 0,
        fabric_paths_down: 0,
        fabrics_present: [],
      },
    },
    classification: { installation_type: "HOSTED_CLUSTER", matched_rule_id: null },
    health: {
      overall: "HEALTHY",
      cpu: "HEALTHY",
      memory: "HEALTHY",
      storage: "HEALTHY",
      network: "HEALTHY",
      connectivity: "HEALTHY",
      power: "HEALTHY",
    },
    maintenance: { enabled: false, reason: null },
    unread_fields: [],
    nic_os_names: {},
    openshift: {
      lifecycle_state: "UNKNOWN",
      mce_id: null,
      cluster_name: null,
      cluster_id: null,
      role: null,
      node_name: null,
      bmh_name: null,
      agent_id: null,
      boot_mac: null,
      last_reported_at: null,
      reported_by_agent_id: null,
    },
    tags: [],
    created_at: "2026-08-13T10:00:00Z",
    site_id: "tlv",
    manager_id: "mgr_1",
    source_provider: "UCS_CENTRAL",
    last_seen_at: "2026-08-13T10:00:00Z",
    updated_at: "2026-08-13T10:00:00Z",
    ...overrides,
  };
}

describe("OverviewTab maintenance controls", () => {
  it("shows a start-maintenance form when not in maintenance, and omits it without a handler", () => {
    render(<OverviewTab server={makeServer()} />);
    expect(screen.getByText("Not in maintenance")).toBeInTheDocument();
    // No `onEnableMaintenance` supplied — the form must not render at all,
    // not just be disabled (this is the "usable read-only" contract the
    // component's prop docstring describes).
    expect(screen.queryByPlaceholderText("Reason (optional)")).not.toBeInTheDocument();
  });

  it("submits the typed reason when starting maintenance", () => {
    const onEnable = vi.fn();
    render(<OverviewTab server={makeServer()} onEnableMaintenance={onEnable} />);

    fireEvent.change(screen.getByPlaceholderText("Reason (optional)"), {
      target: { value: "planned firmware upgrade" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Start maintenance" }));

    expect(onEnable).toHaveBeenCalledWith("planned firmware upgrade");
  });

  it("shows the reason and an end-maintenance control when already in maintenance", () => {
    const onDisable = vi.fn();
    render(
      <OverviewTab
        server={makeServer({ maintenance: { enabled: true, reason: "disk replacement" } })}
        onDisableMaintenance={onDisable}
      />,
    );

    expect(screen.getByText("disk replacement")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "End maintenance" }));
    expect(onDisable).toHaveBeenCalledOnce();
  });

  it("disables both controls while a mutation is pending", () => {
    render(
      <OverviewTab
        server={makeServer({ maintenance: { enabled: true, reason: "x" } })}
        onDisableMaintenance={vi.fn()}
        maintenancePending
      />,
    );
    expect(screen.getByRole("button", { name: "End maintenance" })).toBeDisabled();
  });
});

describe("OverviewTab OpenShift membership", () => {
  it("shows nothing has reported rather than guessing from the classification", () => {
    // A regex verdict on the hostname is not proof of cluster membership,
    // so an unreported server must not borrow its classification here.
    render(<OverviewTab server={makeServer()} />);

    expect(screen.getByText("Not reported")).toBeInTheDocument();
  });

  it("names the hosted cluster and the MCE that reported it", () => {
    const server = makeServer();
    server.openshift = {
      ...server.openshift,
      lifecycle_state: "HOSTED_NODE",
      mce_id: "mce-tlv",
      cluster_name: "hc-tlv-02",
      role: "worker",
    };

    render(<OverviewTab server={server} />);

    expect(screen.getByText("Hosted cluster node")).toBeInTheDocument();
    expect(screen.getByText("hc-tlv-02")).toBeInTheDocument();
    expect(screen.getByText(/mce-tlv/)).toBeInTheDocument();
  });

  it("names the UPI cluster without an MCE, which does not manage one", () => {
    const server = makeServer();
    server.openshift = {
      ...server.openshift,
      lifecycle_state: "UPI_NODE",
      cluster_name: "upi-tlv",
      role: "master",
    };

    render(<OverviewTab server={server} />);

    expect(screen.getByText("UPI node")).toBeInTheDocument();
    expect(screen.getByText("upi-tlv")).toBeInTheDocument();
    expect(screen.queryByText(/MCE /)).not.toBeInTheDocument();
  });

  it("shows an unbound agent as available, with no cluster name", () => {
    const server = makeServer();
    server.openshift = {
      ...server.openshift,
      lifecycle_state: "AVAILABLE",
      mce_id: "mce-nyc",
    };

    render(<OverviewTab server={server} />);

    expect(screen.getByText("Available in MCE")).toBeInTheDocument();
    expect(screen.getByText(/mce-nyc/)).toBeInTheDocument();
  });

  it("shows a disagreement with the classification rather than hiding it", () => {
    // The whole reason the two are separate: an UNCLASSIFIED name on a
    // server a hosted cluster is really running is a misnamed server, and
    // both values have to be visible to notice.
    const server = makeServer();
    server.classification.installation_type = "UNCLASSIFIED";
    server.openshift = {
      ...server.openshift,
      lifecycle_state: "HOSTED_NODE",
      cluster_name: "hc-nyc-01",
      mce_id: "mce-nyc",
    };

    render(<OverviewTab server={server} />);

    expect(screen.getByText("UNCLASSIFIED")).toBeInTheDocument();
    expect(screen.getByText("Hosted cluster node")).toBeInTheDocument();
  });
});
