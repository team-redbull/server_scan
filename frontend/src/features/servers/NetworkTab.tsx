import { LinkStateBadge } from "@/components/LinkStateBadge";
import type { NetworkInfo, NetworkInterface } from "@/types/server";

/** `NIC.Slot.8-1-1` / `NIC.Integrated.1-2-1` -> the kind and its port. */
const FQDD = /^NIC\.([A-Za-z]+)\.(\d+)-(\d+)-(\d+)$/;

/** Where an interface physically is, in words.
 *
 * The stored `location` is `8/1/1`, which is exact and unreadable. This
 * says the same thing, and says "Onboard" rather than "Slot 1" for an
 * integrated NIC — the distinction the OS name hangs off. */
function describeLocation(iface: NetworkInterface): string | null {
  const match = FQDD.exec(iface.name);
  if (!match) return iface.location;
  const [, kind, controller, port] = match;
  const where = kind === "Integrated" ? "Onboard" : `Slot ${controller}`;
  return `${where} · port ${port}`;
}

export function NetworkTab({
  network,
  osNames,
}: {
  network: NetworkInfo | undefined;
  /** FQDD -> OS-level name, for the interfaces a mapping is configured
   * for. Absent entries render as nothing at all, never as a guess. */
  osNames?: Record<string, string>;
}) {
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
            <Stat label="Address" value={bmc.host ?? bmc.address_raw ?? "—"} />
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
          <ul className="mt-2 divide-y divide-gray-100 dark:divide-gray-800">
            {interfaces.map((iface, index) => {
              const osName = osNames?.[iface.name];
              const location = describeLocation(iface);
              return (
                <li key={`${iface.name}-${iface.mac}`} className="py-3">
                  <div className="flex flex-wrap items-baseline justify-between gap-x-4">
                    <span className="font-medium">{iface.name}</span>
                    <span className="font-mono text-sm text-gray-600 dark:text-gray-300">
                      {iface.mac ?? "—"}
                    </span>
                  </div>
                  {/* Every field here dashes rather than disappearing
                   * when it was not read. A missing row reads as "does
                   * not apply"; a dash says the collector looked and got
                   * nothing, which is the distinction this whole codebase
                   * turns on. */}
                  <div className="mt-1 flex flex-wrap items-center gap-x-2 text-sm text-gray-500">
                    <span>{location ?? "—"}</span>
                    <span aria-hidden>·</span>
                    {/* The MAC's position in discovery order. Existing
                     * tooling selects the pair to bond by this ("the
                     * third and fourth MACs"), so it is worth showing
                     * next to the location that supersedes it. */}
                    <span>MAC #{index + 1}</span>
                    <span aria-hidden>·</span>
                    <span>
                      {iface.speed_mbps != null ? `${iface.speed_mbps} Mbps` : "—"}
                    </span>
                    <span aria-hidden>·</span>
                    <LinkStateBadge state={iface.link_state} />
                  </div>
                  {osName && (
                    /* Labelled as derived on purpose: this one is
                     * configuration, not something the BMC reported, and
                     * an operator acting on it should know which. */
                    <p className="mt-1 text-sm text-gray-500">
                      OS name (derived):{" "}
                      <span className="font-mono text-gray-700 dark:text-gray-300">
                        {osName}
                      </span>
                    </p>
                  )}
                </li>
              );
            })}
          </ul>
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
