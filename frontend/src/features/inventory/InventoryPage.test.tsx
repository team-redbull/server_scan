import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createMemoryRouter, RouterProvider } from "react-router";

import { InventoryPage } from "@/features/inventory/InventoryPage";
import type { ServerListResponse, ServerSummary } from "@/types/server";

function makeServer(overrides: Partial<ServerSummary> = {}): ServerSummary {
  return {
    id: "srv_1",
    name: "ocp-dell-worker-001",
    vendor: "dell",
    model: "PowerEdge R760",
    site_id: "site_tlv_01",
    manager_id: "mgr_ome_tlv_01",
    classification: { installation_type: "HOSTED_CLUSTER" },
    health: { overall: "HEALTHY" },
    maintenance: { enabled: false },
    connectivity: {
      facts: { fabric_paths_total: 2, fabric_paths_up: 2, fabric_paths_down: 0, fabrics_present: ["A", "B"] },
    },
    last_seen_at: "2026-08-12T10:00:00Z",
    updated_at: "2026-08-12T10:00:00Z",
    ...overrides,
  };
}

function pageResponse(
  items: ServerSummary[],
  page: Partial<ServerListResponse["page"]> = {},
): ServerListResponse {
  return {
    items,
    page: {
      next_cursor: null,
      has_more: false,
      page_size: 50,
      count: null,
      count_capped: false,
      ...page,
    },
  };
}

function jsonResponse(body: unknown) {
  return Promise.resolve({
    ok: true,
    status: 200,
    json: () => Promise.resolve(body),
  });
}

function renderInventoryPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const router = createMemoryRouter([{ path: "/", element: <InventoryPage /> }], {
    initialEntries: ["/"],
  });

  render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );

  return { router };
}

function lastRequestUrl(fetchMock: ReturnType<typeof vi.fn>): URL {
  const calls = fetchMock.mock.calls;
  const lastCall = calls.at(-1) as [string, RequestInit] | undefined;
  if (!lastCall) {
    throw new Error("fetch was never called");
  }
  return new URL(lastCall[0], "http://localhost");
}

describe("InventoryPage", () => {
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
        pageResponse([
          makeServer(),
          makeServer({ id: "srv_2", name: "ucs-cisco-worker-002", vendor: "cisco", model: "UCS C240" }),
        ]),
      ),
    );

    renderInventoryPage();

    await waitFor(() => {
      expect(screen.getByText("ocp-dell-worker-001")).toBeInTheDocument();
    });
    expect(screen.getByText("ucs-cisco-worker-002")).toBeInTheDocument();
    // Fabric summary derived from connectivity.facts.
    expect(screen.getAllByText("2/2 up")).toHaveLength(2);
  });

  it("updates the URL search params when a filter changes", async () => {
    fetchMock.mockImplementation(() => jsonResponse(pageResponse([makeServer()])));

    const { router } = renderInventoryPage();

    await waitFor(() => {
      expect(screen.getByText("ocp-dell-worker-001")).toBeInTheDocument();
    });

    const vendorSelect = screen.getByLabelText("Vendor");
    fireEvent.change(vendorSelect, { target: { value: "cisco" } });

    await waitFor(() => {
      expect(router.state.location.search).toContain("vendor=cisco");
    });

    await waitFor(() => {
      expect(lastRequestUrl(fetchMock).searchParams.get("vendor")).toBe("cisco");
    });
  });

  it("resets the cursor in the URL when a filter changes", async () => {
    fetchMock.mockImplementation((input: string) => {
      const url = new URL(input, "http://localhost");
      if (url.searchParams.get("cursor") === "cursor-1") {
        return jsonResponse(pageResponse([makeServer({ id: "srv_2", name: "page-two-server" })]));
      }
      return jsonResponse(pageResponse([makeServer()], { has_more: true, next_cursor: "cursor-1" }));
    });

    const { router } = renderInventoryPage();

    await waitFor(() => {
      expect(screen.getByText("ocp-dell-worker-001")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole("button", { name: "Next" }));

    await waitFor(() => {
      expect(router.state.location.search).toContain("cursor=cursor-1");
    });

    const vendorSelect = screen.getByLabelText("Vendor");
    fireEvent.change(vendorSelect, { target: { value: "dell" } });

    await waitFor(() => {
      expect(router.state.location.search).not.toContain("cursor=");
    });
  });

  it("paginates with the next_cursor and disables Next once has_more is false", async () => {
    fetchMock.mockImplementation((input: string) => {
      const url = new URL(input, "http://localhost");
      if (url.searchParams.get("cursor") === "cursor-1") {
        return jsonResponse(
          pageResponse([makeServer({ id: "srv_2", name: "page-two-server" })], {
            has_more: false,
            next_cursor: null,
          }),
        );
      }
      return jsonResponse(pageResponse([makeServer()], { has_more: true, next_cursor: "cursor-1" }));
    });

    renderInventoryPage();

    await waitFor(() => {
      expect(screen.getByText("ocp-dell-worker-001")).toBeInTheDocument();
    });

    const nextButton = screen.getByRole("button", { name: "Next" });
    expect(nextButton).not.toBeDisabled();

    fireEvent.click(nextButton);

    await waitFor(() => {
      expect(screen.getByText("page-two-server")).toBeInTheDocument();
    });

    expect(lastRequestUrl(fetchMock).searchParams.get("cursor")).toBe("cursor-1");
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();
  });

  it("shows the API error detail when the request fails", async () => {
    fetchMock.mockImplementation(() =>
      Promise.resolve({
        ok: false,
        status: 422,
        json: () =>
          Promise.resolve({
            type: "/problems/page-size-too-large",
            title: "Unprocessable Entity",
            status: 422,
            detail: "page_size must be <= 200",
            instance: "/api/v1/servers",
            code: "PAGE_SIZE_TOO_LARGE",
            request_id: "req_123",
            details: {},
          }),
      }),
    );

    renderInventoryPage();

    await waitFor(() => {
      expect(screen.getByText("page_size must be <= 200")).toBeInTheDocument();
    });
  });
});
