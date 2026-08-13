import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ClassificationRulesPage } from "@/features/classification/ClassificationRulesPage";
import type { ClassificationRuleListResponse, ClassificationRuleResponse } from "@/types/classification";

function makeRule(overrides: Partial<ClassificationRuleResponse> = {}): ClassificationRuleResponse {
  return {
    id: "crul_1",
    name: "dell-vendor-hosted-cluster",
    description: "Dell hosted-cluster naming convention.",
    enabled: true,
    system: false,
    installation_type: "HOSTED_CLUSTER",
    scope: { vendor: "dell", manager_type: null, site_id: null },
    field: "name",
    pattern: "^ocp-dell-.*",
    flags: { ignore_case: true, multiline: false, dotall: false },
    source: "VENDOR_CUSTOM",
    priority: 300,
    order: 0,
    stats: { match_count: 0, last_matched_at: null, timeout_count: 0, quarantined: false },
    revision: 1,
    created_at: "2026-08-12T10:00:00Z",
    updated_at: "2026-08-12T10:00:00Z",
    created_by: null,
    updated_by: null,
    ...overrides,
  };
}

function listResponse(items: ClassificationRuleResponse[]): ClassificationRuleListResponse {
  return { items };
}

function jsonResponse(body: unknown) {
  return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) });
}

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/classification-rules"]}>
        <ClassificationRulesPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function lastRequestUrl(fetchMock: ReturnType<typeof vi.fn>): URL {
  const calls = fetchMock.mock.calls;
  const lastCall = calls.at(-1) as [string, RequestInit] | undefined;
  if (!lastCall) {
    throw new Error("fetch was never called");
  }
  return new URL(lastCall[0], "http://localhost");
}

describe("ClassificationRulesPage", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders rows from the mocked API response", async () => {
    fetchMock.mockImplementation(() =>
      jsonResponse(
        listResponse([
          makeRule(),
          makeRule({ id: "crul_2", name: "dell-vendor-upi", installation_type: "UPI", enabled: false }),
        ]),
      ),
    );

    renderPage();

    await waitFor(() => {
      expect(screen.getByText("dell-vendor-hosted-cluster")).toBeInTheDocument();
    });
    expect(screen.getByText("dell-vendor-upi")).toBeInTheDocument();
    expect(screen.getByText("enabled")).toBeInTheDocument();
    expect(screen.getByText("disabled")).toBeInTheDocument();
  });

  it("sends the enabled query param when the status filter changes", async () => {
    fetchMock.mockImplementation(() => jsonResponse(listResponse([makeRule()])));

    renderPage();

    await waitFor(() => {
      expect(screen.getByText("dell-vendor-hosted-cluster")).toBeInTheDocument();
    });

    const statusSelect = screen.getByLabelText("Status");
    fireEvent.change(statusSelect, { target: { value: "true" } });

    await waitFor(() => {
      expect(lastRequestUrl(fetchMock).searchParams.get("enabled")).toBe("true");
    });

    fireEvent.change(statusSelect, { target: { value: "false" } });

    await waitFor(() => {
      expect(lastRequestUrl(fetchMock).searchParams.get("enabled")).toBe("false");
    });
  });

  it("shows an empty state when no rules match the filter", async () => {
    fetchMock.mockImplementation(() => jsonResponse(listResponse([])));

    renderPage();

    await waitFor(() => {
      expect(screen.getByText("No classification rules match the current filter.")).toBeInTheDocument();
    });
  });
});
