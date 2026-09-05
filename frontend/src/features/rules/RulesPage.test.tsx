import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { RulesPage } from "@/features/rules/RulesPage";

const RULES_RESPONSE = {
  items: [
    {
      id: "rule_1",
      name: "hypershift hostname",
      installation_type: "HOSTED_CLUSTER",
      source: "SYSTEM_DEFAULT",
      priority: 100,
      enabled: true,
      system: true,
      scope: { vendor: null, manager_type: null, site_id: "tlv" },
    },
    {
      id: "rule_2",
      name: "retired convention",
      installation_type: "UPI",
      source: "SYSTEM_DEFAULT",
      priority: 50,
      enabled: false,
      system: true,
      scope: { vendor: null, manager_type: null, site_id: null },
    },
  ],
};

const POLICIES_RESPONSE = {
  items: [
    {
      id: "policy_1",
      name: "failed drive",
      category: "storage",
      severity: "CRITICAL",
      policy_key: "storage.failed_drive",
      mode: "THRESHOLD",
      enabled: true,
      system: true,
      scope: { vendor: "dell", manager_type: null, site_id: null },
    },
  ],
};

function jsonResponse(body: unknown) {
  return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) });
}

function renderRulesPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <RulesPage />
    </QueryClientProvider>,
  );
}

describe("RulesPage", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn((input: string) => {
      const url = new URL(input, "http://localhost");
      if (url.pathname === "/api/v1/classification-rules") {
        return jsonResponse(RULES_RESPONSE);
      }
      if (url.pathname === "/api/v1/health-policies") {
        return jsonResponse(POLICIES_RESPONSE);
      }
      throw new Error(`unexpected request to ${url.pathname}`);
    });
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("shows classification rules and health policies on one page", async () => {
    // The two are read together far more often than either alone: a
    // server's installation type decides which policies apply to it.
    renderRulesPage();

    expect(await screen.findByText("hypershift hostname")).toBeInTheDocument();
    expect(await screen.findByText("failed drive")).toBeInTheDocument();
  });

  it("offers no way to create, edit, enable or delete anything", async () => {
    // The whole point. A rule that exists in one estate and not another
    // makes two installations classify the same server differently, so
    // the UI must not be a place one can be added.
    renderRulesPage();
    await screen.findByText("hypershift hostname");

    // By role, not by text: "enabled"/"disabled" appear as status badges
    // on every row, so matching those words would fail on the page's own
    // legitimate content. What must not exist is anything interactive.
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
    expect(screen.queryByText(/new rule/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/new policy/i)).not.toBeInTheDocument();
  });

  it("still shows a disabled rule rather than hiding it", async () => {
    // Disabled is part of the shipped configuration, not a local edit, so
    // hiding it would misrepresent what this deployment runs.
    renderRulesPage();

    expect(await screen.findByText("retired convention")).toBeInTheDocument();
    expect(screen.getByText("disabled")).toBeInTheDocument();
  });

  it("renders an unscoped rule as unscoped rather than blank", async () => {
    renderRulesPage();

    expect(await screen.findByText("(unscoped)")).toBeInTheDocument();
    expect(screen.getByText("site=tlv")).toBeInTheDocument();
    expect(screen.getByText("vendor=dell")).toBeInTheDocument();
  });
});
