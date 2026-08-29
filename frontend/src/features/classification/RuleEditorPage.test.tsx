import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { RuleEditorPage } from "@/features/classification/RuleEditorPage";

function renderNewRulePage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const router = createMemoryRouter(
    [{ path: "/classification-rules/new", element: <RuleEditorPage /> }],
    { initialEntries: ["/classification-rules/new"] },
  );

  render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
}

describe("RuleEditorPage (create)", () => {
  beforeEach(() => {
    // No rule/preview fetches happen on the "new" page until field+pattern
    // are both filled in, but stub fetch defensively anyway.
    vi.stubGlobal("fetch", vi.fn());
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("defaults to GLOBAL_CUSTOM with no scope and shows its priority band", async () => {
    renderNewRulePage();

    await waitFor(() => {
      expect(screen.getByText("New Classification Rule")).toBeInTheDocument();
    });

    expect(screen.getByText("GLOBAL_CUSTOM rules have no scope (unscoped).")).toBeInTheDocument();
    expect(screen.getByText("GLOBAL_CUSTOM: 200-299")).toBeInTheDocument();
  });

  it("shows a site field and the SITE_CUSTOM band when source changes to SITE_CUSTOM", async () => {
    renderNewRulePage();

    await waitFor(() => {
      expect(screen.getByText("New Classification Rule")).toBeInTheDocument();
    });

    fireEvent.change(screen.getByLabelText("Source"), { target: { value: "SITE_CUSTOM" } });

    expect(screen.getByLabelText("Site")).toBeInTheDocument();
    expect(screen.getByText("SITE_CUSTOM: 500-599")).toBeInTheDocument();
    expect(screen.queryByLabelText("Vendor")).not.toBeInTheDocument();
  });

  it("shows a vendor field and the VENDOR_CUSTOM band when source changes to VENDOR_CUSTOM", async () => {
    renderNewRulePage();

    await waitFor(() => {
      expect(screen.getByText("New Classification Rule")).toBeInTheDocument();
    });

    fireEvent.change(screen.getByLabelText("Source"), { target: { value: "VENDOR_CUSTOM" } });

    expect(screen.getByLabelText("Vendor")).toBeInTheDocument();
    expect(screen.getByText("VENDOR_CUSTOM: 300-399")).toBeInTheDocument();
    expect(screen.queryByLabelText("Site")).not.toBeInTheDocument();
  });

  it("shows a manager type field when source changes to MANAGER_CUSTOM", async () => {
    renderNewRulePage();

    await waitFor(() => {
      expect(screen.getByText("New Classification Rule")).toBeInTheDocument();
    });

    fireEvent.change(screen.getByLabelText("Source"), { target: { value: "MANAGER_CUSTOM" } });

    expect(screen.getByLabelText("Manager type")).toBeInTheDocument();
    expect(screen.getByText("MANAGER_CUSTOM: 400-499")).toBeInTheDocument();
  });

  it("does not offer SYSTEM_DEFAULT as a source choice on create", async () => {
    renderNewRulePage();

    await waitFor(() => {
      expect(screen.getByText("New Classification Rule")).toBeInTheDocument();
    });

    const sourceSelect = screen.getByLabelText("Source") as HTMLSelectElement;
    const optionValues = [...sourceSelect.options].map((o) => o.value);
    expect(optionValues).not.toContain("SYSTEM_DEFAULT");
  });
});
