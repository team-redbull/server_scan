import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { PreviewPanel } from "@/features/classification/PreviewPanel";
import type { ClassificationPreviewRequest, ClassificationPreviewResponse } from "@/types/classification";

function jsonResponse(body: unknown) {
  return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) });
}

function renderPanel(request: ClassificationPreviewRequest | null) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <PreviewPanel request={request} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("PreviewPanel (classification)", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("shows the 'not enough info' state when the request is null", () => {
    renderPanel(null);

    expect(
      screen.getByText("Fill in field and pattern above to see a live preview of matching servers."),
    ).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("shows matched_count and the sample list from a mocked preview response", async () => {
    const response: ClassificationPreviewResponse = {
      matched_count: 2,
      truncated: false,
      sample: [
        { id: "srv_1", name: "ocp-dell-worker-001" },
        { id: "srv_2", name: "ocp-dell-worker-002" },
      ],
      mode: "sampled",
    };
    fetchMock.mockImplementation(() => jsonResponse(response));

    renderPanel({ field: "name", pattern: "^ocp-dell-.*" });

    await waitFor(() => {
      expect(screen.getByText("ocp-dell-worker-001")).toBeInTheDocument();
    });
    expect(screen.getByText("ocp-dell-worker-002")).toBeInTheDocument();
    expect(screen.getByText("2")).toBeInTheDocument();
  });

  it("flags a truncated sample", async () => {
    const response: ClassificationPreviewResponse = {
      matched_count: 500,
      truncated: true,
      sample: [{ id: "srv_1", name: "ocp-dell-worker-001" }],
      mode: "sampled",
    };
    fetchMock.mockImplementation(() => jsonResponse(response));

    renderPanel({ field: "name", pattern: "^ocp-dell-.*" });

    await waitFor(() => {
      expect(screen.getByText("(sample truncated)")).toBeInTheDocument();
    });
  });
});
