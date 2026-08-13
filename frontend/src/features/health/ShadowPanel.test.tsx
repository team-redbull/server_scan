import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ShadowPanel } from "@/features/health/ShadowPanel";
import type { HealthPolicyResponse } from "@/types/health";

function makePolicy(overrides: Partial<HealthPolicyResponse> = {}): HealthPolicyResponse {
  return {
    id: "hpol_1",
    name: "UCS fabric path down (warning)",
    description: "",
    enabled: true,
    system: true,
    policy_key: "connectivity.fabric_paths_down_warning",
    mode: "EVALUATE",
    category: "connectivity",
    severity: "WARNING",
    condition: { metric: "connectivity.fabric_paths_down", operator: "EQ", value: 1 },
    evidence: [],
    message_template: "{down} fabric path is down",
    scope: { site_id: null, vendor: null, manager_type: null },
    source: "SYSTEM_DEFAULT",
    priority: 100,
    order: 0,
    stats: { fire_count: 0, last_fired_at: null, error_count: 0, quarantined: false },
    revision: 1,
    created_at: "2026-08-12T10:00:00Z",
    updated_at: "2026-08-12T10:00:00Z",
    created_by: null,
    updated_by: null,
    ...overrides,
  };
}

describe("ShadowPanel", () => {
  it("renders nothing when policy_key is empty", () => {
    render(<ShadowPanel policies={[makePolicy()]} policyKey="" />);
    expect(screen.queryByTestId("shadow-panel")).not.toBeInTheDocument();
  });

  it("renders nothing when no other policy shares the policy_key", () => {
    render(
      <ShadowPanel
        policies={[makePolicy({ id: "hpol_1", policy_key: "connectivity.fabric_paths_down_warning" })]}
        policyKey="connectivity.fabric_paths_down_warning"
        currentId="hpol_1"
      />,
    );
    expect(screen.queryByTestId("shadow-panel")).not.toBeInTheDocument();
  });

  it("lists sibling policies sharing the same policy_key, excluding the one being edited", () => {
    const shared = "connectivity.fabric_paths_down_warning";
    const policies = [
      makePolicy({ id: "hpol_1", name: "System default", policy_key: shared, source: "SYSTEM_DEFAULT" }),
      makePolicy({
        id: "hpol_2",
        name: "Site override — TLV",
        policy_key: shared,
        source: "SITE_CUSTOM",
        priority: 500,
        scope: { site_id: "site_tlv_01", vendor: null, manager_type: null },
      }),
      makePolicy({ id: "hpol_3", name: "Unrelated policy", policy_key: "some.other.key" }),
    ];

    render(<ShadowPanel policies={policies} policyKey={shared} currentId="hpol_1" />);

    expect(screen.getByTestId("shadow-panel")).toBeInTheDocument();
    expect(screen.getByText("This policy shares its policy_key with 1 other policy.")).toBeInTheDocument();
    expect(screen.getByText("Site override — TLV", { exact: false })).toBeInTheDocument();
    expect(screen.queryByText("Unrelated policy", { exact: false })).not.toBeInTheDocument();
    // The policy being edited itself is excluded from its own sibling list.
    expect(screen.queryByText("System default", { exact: false })).not.toBeInTheDocument();
  });
});
