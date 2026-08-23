import { HealthBadge } from "@/components/HealthBadge";
import type { HardwareInfo } from "@/types/server";

const BYTE_UNITS = ["B", "KB", "MB", "GB", "TB", "PB"] as const;

function formatBytes(bytes: number): string {
  if (bytes <= 0) {
    return "0 B";
  }
  const exponent = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), BYTE_UNITS.length - 1);
  const value = bytes / 1024 ** exponent;
  const unit = BYTE_UNITS[exponent] ?? "B";
  return `${value.toFixed(exponent === 0 ? 0 : 1)} ${unit}`;
}

export function HardwareTab({ hardware }: { hardware: HardwareInfo | undefined }) {
  if (!hardware) {
    return <p className="text-gray-500">No hardware data available.</p>;
  }

  const { cpu, memory, storage, gpus, power } = hardware;

  return (
    <div className="space-y-8">
      <section>
        <h2 className="text-sm font-semibold uppercase tracking-wide text-gray-500">CPU</h2>
        {cpu ? (
          <dl className="mt-2 grid grid-cols-2 gap-2 text-sm sm:grid-cols-4">
            <Stat label="Model" value={cpu.model} />
            <Stat label="Sockets" value={String(cpu.sockets)} />
            <Stat label="Cores" value={String(cpu.cores)} />
            <Stat label="Threads" value={String(cpu.threads)} />
          </dl>
        ) : (
          <p className="mt-2 text-sm text-gray-500">No CPU data.</p>
        )}
      </section>

      <section>
        <h2 className="text-sm font-semibold uppercase tracking-wide text-gray-500">Memory</h2>
        {memory ? (
          <p className="mt-2 text-sm">
            {formatBytes(memory.total_bytes)} total ({memory.modules.length} module
            {memory.modules.length === 1 ? "" : "s"})
          </p>
        ) : (
          <p className="mt-2 text-sm text-gray-500">No memory data.</p>
        )}
      </section>

      <section>
        <h2 className="text-sm font-semibold uppercase tracking-wide text-gray-500">Storage</h2>
        {storage && storage.drives.length > 0 ? (
          <>
            <table className="mt-2 min-w-full divide-y divide-gray-200 text-sm dark:divide-gray-700">
              <thead>
                <tr className="text-left text-gray-500">
                  <th className="py-1 pr-4">Model</th>
                  <th className="py-1 pr-4">Serial</th>
                  <th className="py-1 pr-4">Media</th>
                  <th className="py-1 pr-4">Capacity</th>
                  <th className="py-1 pr-4">Health</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100 dark:divide-gray-800">
                {storage.drives.map((drive) => (
                  <tr key={drive.id}>
                    <td className="py-1 pr-4">{drive.model}</td>
                    <td className="py-1 pr-4">{drive.serial}</td>
                    <td className="py-1 pr-4">{drive.media_type}</td>
                    <td className="py-1 pr-4">{formatBytes(drive.capacity_bytes)}</td>
                    <td className="py-1 pr-4">
                      <HealthBadge severity={drive.health} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="mt-2 text-xs text-gray-500">Total: {formatBytes(storage.total_bytes)}</p>
          </>
        ) : (
          <p className="mt-2 text-sm text-gray-500">No storage data.</p>
        )}
      </section>

      <section>
        <h2 className="text-sm font-semibold uppercase tracking-wide text-gray-500">GPU</h2>
        {gpus && gpus.length > 0 ? (
          <div className="mt-2 overflow-x-auto">
            <table className="min-w-full divide-y divide-gray-200 text-sm dark:divide-gray-700">
              <thead>
                <tr className="text-left text-gray-500">
                  <th className="py-1 pr-4">Vendor</th>
                  <th className="py-1 pr-4">Model</th>
                  <th className="py-1 pr-4">Serial</th>
                  <th className="py-1 pr-4">VRAM</th>
                  <th className="py-1 pr-4">Memory type</th>
                  <th className="py-1 pr-4">ECC</th>
                  <th className="py-1 pr-4">Errors (correctable/uncorrectable)</th>
                  <th className="py-1 pr-4">Temp</th>
                  <th className="py-1 pr-4">Power</th>
                  <th className="py-1 pr-4">Health</th>
                  <th className="py-1 pr-4">Firmware</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100 dark:divide-gray-800">
                {gpus.map((gpu, index) => (
                  <tr key={`${gpu.serial ?? gpu.model ?? "gpu"}-${index}`}>
                    <td className="py-1 pr-4">{gpu.vendor ?? "—"}</td>
                    <td className="py-1 pr-4">{gpu.model ?? "Unknown model"}</td>
                    <td className="py-1 pr-4">{gpu.serial ?? "—"}</td>
                    <td className="py-1 pr-4">
                      {gpu.memory_bytes !== undefined ? formatBytes(gpu.memory_bytes) : "—"}
                    </td>
                    <td className="py-1 pr-4">{gpu.memory_type ?? "—"}</td>
                    <td className="py-1 pr-4">
                      {gpu.ecc_mode_enabled === undefined
                        ? "—"
                        : gpu.ecc_mode_enabled
                          ? "On"
                          : "Off"}
                    </td>
                    <td className="py-1 pr-4">
                      {gpu.correctable_error_count ?? "—"} / {gpu.uncorrectable_error_count ?? "—"}
                    </td>
                    <td className="py-1 pr-4">
                      {gpu.temperature_celsius !== undefined
                        ? `${gpu.temperature_celsius.toFixed(0)}°C`
                        : "—"}
                    </td>
                    <td className="py-1 pr-4">
                      {gpu.power_watts !== undefined ? `${gpu.power_watts.toFixed(0)}W` : "—"}
                    </td>
                    <td className="py-1 pr-4">
                      {gpu.health ? <HealthBadge severity={gpu.health} /> : "—"}
                    </td>
                    <td className="py-1 pr-4">{gpu.firmware_version ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="mt-2 text-sm text-gray-500">No GPUs.</p>
        )}
      </section>

      <section>
        <h2 className="text-sm font-semibold uppercase tracking-wide text-gray-500">Power</h2>
        {power && power.psus.length > 0 ? (
          <ul className="mt-2 list-disc pl-5 text-sm">
            {power.psus.map((psu, index) => (
              <li key={psu.id ?? `psu-${index}`}>
                {psu.status ?? "unknown"}
                {psu.watts ? ` — ${psu.watts}W` : ""}
              </li>
            ))}
          </ul>
        ) : (
          <p className="mt-2 text-sm text-gray-500">No power supply data.</p>
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
