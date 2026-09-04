import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Breakdown, SiteStats } from "@/api/sites";
import { SitesOverviewPage } from "@/features/sites/SitesOverviewPage";

function breakdown(overrides: Partial<Breakdown> = {}): Breakdown {
  return {
    total: 0,
    by_vendor: [
      { vendor: "dell", count: 0 },
      { vendor: "cisco", count: 0 },
      { vendor: "hp", count: 0 },
      { vendor: "standalone", count: 0 },
    ],
    by_health: { UNKNOWN: 0, HEALTHY: 0, INFO: 0, WARNING: 0, CRITICAL: 0 },
    in_maintenance: 0,
    ...overrides,
  };
}

function site(
  site_id: string,
  name: string,
  slices: { UPI: Partial<Breakdown>; HOSTED_CLUSTER: Partial<Breakdown> },
): SiteStats {
  const upi = breakdown(slices.UPI);
  const hosted = breakdown(slices.HOSTED_CLUSTER);
  return {
    site_id,
    name,
    ...breakdown({
      total: upi.total + hosted.total,
      by_health: {
        UNKNOWN: 0,
        HEALTHY: 0,
        INFO: 0,
        WARNING: 0,
        CRITICAL: upi.by_health.CRITICAL + hosted.by_health.CRITICAL,
      },
    }),
    by_installation_type: {
      UPI: upi,
      HOSTED_CLUSTER: hosted,
      UNCLASSIFIED: breakdown(),
    },
  };
}

const SITES_RESPONSE = {
  items: [
    site("tlv", "Tel Aviv", {
      UPI: { total: 30, by_health: { UNKNOWN: 0, HEALTHY: 28, INFO: 0, WARNING: 0, CRITICAL: 2 } },
      HOSTED_CLUSTER: { total: 12 },
    }),
    site("nyc", "New York City", {
      UPI: { total: 5 },
      HOSTED_CLUSTER: { total: 3, by_health: { UNKNOWN: 0, HEALTHY: 2, INFO: 0, WARNING: 0, CRITICAL: 1 } },
    }),
  ],
};

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const router = createMemoryRouter([{ path: "/", element: <SitesOverviewPage /> }], {
    initialEntries: ["/"],
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
}

/** The card whose heading is `name`, as a link. */
function card(name: string): HTMLAnchorElement {
  const heading = screen.getByRole("heading", { name });
  const link = heading.closest("a");
  expect(link).not.toBeNull();
  return link as HTMLAnchorElement;
}

describe("SitesOverviewPage", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve(SITES_RESPONSE),
        }),
      ),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("sums the three fleet-wide cards from the per-site slices", async () => {
    renderPage();

    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "Across all sites" })).toBeInTheDocument();
    });

    expect(within(card("Across all sites")).getByText("50")).toBeInTheDocument();
    expect(within(card("UPI")).getByText("35")).toBeInTheDocument();
    expect(within(card("Hosted cluster")).getByText("15")).toBeInTheDocument();
  });

  it("links each fleet-wide card to the matching pre-filtered server list", async () => {
    renderPage();

    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "UPI" })).toBeInTheDocument();
    });

    expect(card("Across all sites")).toHaveAttribute("href", "/servers");
    expect(card("UPI")).toHaveAttribute("href", "/servers?installation_type=UPI");
    expect(card("Hosted cluster")).toHaveAttribute(
      "href",
      "/servers?installation_type=HOSTED_CLUSTER",
    );
  });

  it("carries health into the installation-type cards", async () => {
    renderPage();

    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "UPI" })).toBeInTheDocument();
    });

    // Only TLV's UPI servers are critical; NYC's critical one is hosted.
    expect(within(card("UPI")).getByText("2")).toBeInTheDocument();
    expect(within(card("UPI")).getByText(/critical/)).toBeInTheDocument();
    expect(within(card("Hosted cluster")).getByText("1")).toBeInTheDocument();
  });

  it("still renders one card per site, linked by site_id", async () => {
    renderPage();

    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "Tel Aviv" })).toBeInTheDocument();
    });

    expect(card("Tel Aviv")).toHaveAttribute("href", "/servers?site_id=tlv");
    expect(card("New York City")).toHaveAttribute("href", "/servers?site_id=nyc");
  });
});
