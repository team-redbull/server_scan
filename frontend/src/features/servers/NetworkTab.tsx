import { LinkStateBadge } from "@/components/LinkStateBadge";
import type { NetworkInfo } from "@/types/server";

export function NetworkTab({ network }: { network: NetworkInfo | undefined }) {
  if (!network) {
    return <p className="text-gray-500">No network data available.</p>;
  }

  const { bmc, interfaces } = network;

  return (
    <div className="space-y-8">
      <section>
        <h2 className="text-sm font-semibold uppercase tracking-wide text-gray-500">
          BMC
        </h2>
        {bmc ? (
          <dl className="mt-2 grid grid-cols-2 gap-2 text-sm sm:grid-cols-4">
            {/* The host alone: an operator wants the address they would
             * ping or open, not the scheme, port and Redfish path the
             * collector reported. The full URI is still stored, for the
             * Metal3 `BareMetalHost` round-trip. */}
            <Stat label="Address" value={bmc.host ?? bmc.address_raw} />
            {bmc.mac && <Stat label="MAC" value={bmc.mac} />}
          </dl>
        ) : (
          <p className="mt-2 text-sm text-gray-500">No BMC data.</p>
        )}
      </section>

      <section>
        <h2 className="text-sm font-semibold uppercase tracking-wide text-gray-500">
          Interfaces
        </h2>
        {interfaces.length > 0 ? (
          <table className="mt-2 min-w-full divide-y divide-gray-200 text-sm dark:divide-gray-700">
            <thead>
              <tr className="text-left text-gray-500">
                <th className="py-1 pr-4">Name</th>
                <th className="py-1 pr-4">Location</th>
                <th className="py-1 pr-4">MAC</th>
                <th className="py-1 pr-4">Speed</th>
                <th className="py-1 pr-4">Link state</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100 dark:divide-gray-800">
              {interfaces.map((iface) => (
                <tr key={`${iface.name}-${iface.mac}`}>
                  <td className="py-1 pr-4">{iface.name}</td>
                  <td className="py-1 pr-4">{iface.location ?? "—"}</td>
                  <td className="py-1 pr-4">{iface.mac}</td>
                  <td className="py-1 pr-4">
                    {iface.speed_mbps ? `${iface.speed_mbps} Mbps` : "—"}
                  </td>
                  <td className="py-1 pr-4">
                    <LinkStateBadge state={iface.link_state} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p className="mt-2 text-sm text-gray-500">No network interfaces.</p>
        )}
      </section>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-xs text-gray-500">{label}</dt>
      <dd className="font-medium">{value}</dd>
    </div>
  );
}
