import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { NetworkTab } from "@/features/servers/NetworkTab";
import type { NetworkInfo } from "@/types/server";

/** A BMC that answered, and one interface it could not read a MAC or a
 * speed for — every field here is `X | None` on the backend, so `null` is
 * what arrives, not a missing key. */
function partialNetwork(): NetworkInfo {
  return {
    bmc: { address_raw: null, scheme: null, host: null, port: null, mac: null },
    interfaces: [
      { name: "NIC.Slot.1-1", mac: null, speed_mbps: null, link_state: "UNKNOWN", location: null },
    ],
  };
}

describe("NetworkTab unread fields", () => {
  it("dashes a BMC address and a NIC MAC the collector could not read", () => {
    render(<NetworkTab network={partialNetwork()} />);

    expect(screen.getByText("NIC.Slot.1-1")).toBeInTheDocument();
    // Address, location, MAC and speed — all four degrade to the dash.
    expect(screen.getAllByText("—")).toHaveLength(4);
  });

  it("prefers the BMC host over its raw address when both were read", () => {
    const network = partialNetwork();
    network.bmc = {
      address_raw: "https://10.0.0.5:443/redfish/v1",
      scheme: "https",
      host: "10.0.0.5",
      port: 443,
      mac: "aa:bb:cc:dd:ee:ff",
    };

    render(<NetworkTab network={network} />);

    expect(screen.getByText("10.0.0.5")).toBeInTheDocument();
    expect(screen.getByText("aa:bb:cc:dd:ee:ff")).toBeInTheDocument();
  });
});
