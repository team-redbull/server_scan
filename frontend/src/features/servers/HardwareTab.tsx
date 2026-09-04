import type { ReactNode } from "react";

import { HealthBadge } from "@/components/HealthBadge";
import { isHealthSeverity } from "@/components/severity";
import type { ComponentHealth, HardwareInfo } from "@/types/server";

const BYTE_UNITS = ["B", "KB", "MB", "GB", "TB", "PB"] as const;

/** A field the last collection could not read, with nothing stored from an
 * earlier one: the `0`/`[]` below it is the model's zero, not a reading. */
const NOT_READ_TITLE = "The most recent collection could not read this.";

/** Read on an earlier run and carried forward — real data, just not
 * confirmed by the latest collection. Shown, dimmed, never hidden. */
const STALE_TITLE = "Not confirmed by the most recent collection.";

function formatBytes(bytes: number): string {
  if (bytes <= 0) {
    return "0 B";
  }
  const exponent = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), BYTE_UNITS.length - 1);
  const value = bytes / 1024 ** exponent;
  const unit = BYTE_UNITS[exponent] ?? "B";
  return `${value.toFixed(exponent === 0 ? 0 : 1)} ${unit}`;
}

export function HardwareTab({
  hardware,
  unreadFields = [],
}: {
  hardware: HardwareInfo | undefined;
  /** `ServerDetail.unread_fields` — dotted paths the most recent collection
   * could not read. Optional so the tab still renders for a caller with no
   * such list (a document written before the field existed). */
  unreadFields?: string[] | undefined;
}) {
  if (!hardware) {
    return <p className="text-gray-500">No hardware data available.</p>;
  }

  const unread = new Set(unreadFields);
  const { cpu, memory, storage, gpus, power } = hardware;

  return (
    <div className="space-y-8">
      <section>
        <h2 className="text-sm font-semibold uppercase tracking-wide text-gray-500">CPU</h2>
        {cpu ? (
          <dl className="mt-2 grid grid-cols-2 gap-2 text-sm sm:grid-cols-4">
            <Stat label="Model" value={cpu.model} unread={unread.has("hardware.cpu.model")} />
            <Stat label="Sockets" value={cpu.sockets} unread={unread.has("hardware.cpu.sockets")} />
            <Stat label="Cores" value={cpu.cores} unread={unread.has("hardware.cpu.cores")} />
            <Stat label="Threads" value={cpu.threads} unread={unread.has("hardware.cpu.threads")} />
          </dl>
        ) : (
          <p className="mt-2 text-sm text-gray-500">No CPU data.</p>
        )}
      </section>

      <section>
        <h2 className="text-sm font-semibold uppercase tracking-wide text-gray-500">Memory</h2>
        <Reported
          unread={unread.has("hardware.memory.total_bytes")}
          empty={!memory || memory.total_bytes <= 0}
        >
          {memory ? (
            <p className="mt-2 text-sm">
              {formatBytes(memory.total_bytes)} total ({memory.modules.length} module
              {memory.modules.length === 1 ? "" : "s"})
            </p>
          ) : (
            <p className="mt-2 text-sm text-gray-500">No memory data.</p>
          )}
        </Reported>
      </section>

      <section>
        <h2 className="text-sm font-semibold uppercase tracking-wide text-gray-500">Storage</h2>
        <Reported
          unread={unread.has("hardware.storage.drives")}
          empty={!storage || storage.drives.length === 0}
        >
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
                      <td className="py-1 pr-4">{drive.model ?? "—"}</td>
                      <td className="py-1 pr-4">{drive.serial ?? "—"}</td>
                      <td className="py-1 pr-4">{drive.media_type}</td>
                      <td className="py-1 pr-4">
                        {drive.capacity_bytes != null ? formatBytes(drive.capacity_bytes) : "—"}
                      </td>
                      <td className="py-1 pr-4">
                        <Health value={drive.health} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <p className="mt-2 text-xs text-gray-500">
                Total:{" "}
                <Reported
                  inline
                  unread={unread.has("hardware.storage.total_bytes")}
                  empty={storage.total_bytes <= 0}
                >
                  {formatBytes(storage.total_bytes)}
                </Reported>
              </p>
            </>
          ) : (
            <p className="mt-2 text-sm text-gray-500">No storage data.</p>
          )}
        </Reported>
      </section>

      <section>
        <h2 className="text-sm font-semibold uppercase tracking-wide text-gray-500">GPU</h2>
        <Reported unread={unread.has("hardware.gpus")} empty={!gpus || gpus.length === 0}>
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
                        {gpu.memory_bytes != null ? formatBytes(gpu.memory_bytes) : "—"}
                      </td>
                      <td className="py-1 pr-4">{gpu.memory_type ?? "—"}</td>
                      <td className="py-1 pr-4">
                        {gpu.ecc_mode_enabled == null ? "—" : gpu.ecc_mode_enabled ? "On" : "Off"}
                      </td>
                      <td className="py-1 pr-4">
                        {gpu.correctable_error_count ?? "—"} / {gpu.uncorrectable_error_count ?? "—"}
                      </td>
                      <td className="py-1 pr-4">
                        {gpu.temperature_celsius != null
                          ? `${gpu.temperature_celsius.toFixed(0)}°C`
                          : "—"}
                      </td>
                      <td className="py-1 pr-4">
                        {gpu.power_watts != null ? `${gpu.power_watts.toFixed(0)}W` : "—"}
                      </td>
                      <td className="py-1 pr-4">
                        <Health value={gpu.health} />
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
        </Reported>
      </section>

      <section>
        <h2 className="text-sm font-semibold uppercase tracking-wide text-gray-500">Power</h2>
        <Reported
          unread={unread.has("hardware.power.psus")}
          empty={!power || power.psus.length === 0}
        >
          {power && power.psus.length > 0 ? (
            <ul className="mt-2 list-disc pl-5 text-sm">
              {power.psus.map((psu, index) => (
                <li key={psu.id || `psu-${index}`}>
                  {psu.model ?? psu.id ?? "—"}
                  {psu.capacity_watts != null ? ` — ${psu.capacity_watts}W` : ""}{" "}
                  <Health value={psu.health} />
                </li>
              ))}
            </ul>
          ) : (
            <p className="mt-2 text-sm text-gray-500">No power supply data.</p>
          )}
        </Reported>
      </section>
    </div>
  );
}

/**
 * Renders a block honestly when the last collection could not read it:
 * "Not reported" in place of the zero it would otherwise state as fact, or
 * the carried-forward value dimmed when there is one. A read field renders
 * its children untouched.
 */
function Reported({
  unread,
  empty,
  inline = false,
  children,
}: {
  unread: boolean;
  /** Whether the stored value is the model's zero (`0`, `[]`) — the case
   * where showing it at all would be a claim no collector ever made. */
  empty: boolean;
  /** Render inside a line of text rather than as its own block. */
  inline?: boolean;
  children: ReactNode;
}) {
  if (!unread) {
    return <>{children}</>;
  }
  if (empty) {
    return inline ? (
      <span className="text-gray-500" title={NOT_READ_TITLE}>
        Not reported
      </span>
    ) : (
      <p className="mt-2 text-sm text-gray-500" title={NOT_READ_TITLE}>
        Not reported
      </p>
    );
  }
  const Tag = inline ? "span" : "div";
  return (
    <Tag className="opacity-50" title={STALE_TITLE}>
      {children}
    </Tag>
  );
}

/**
 * One component's own reported condition. Badged when it is a severity
 * this UI can style, shown as the collector's raw word when it is not
 * (Cisco reports UP/DOWN here, not HEALTHY/CRITICAL), and dashed when the
 * collector read nothing at all.
 */
function Health({ value }: { value: ComponentHealth }) {
  if (value == null) {
    return <>—</>;
  }
  return isHealthSeverity(value) ? <HealthBadge severity={value} /> : <>{value}</>;
}

function Stat({
  label,
  value,
  unread = false,
}: {
  label: string;
  value: string | number | null | undefined;
  unread?: boolean;
}) {
  // `0` and `""` are the zero values `_carry_forward` writes for a field
  // nobody has ever read — falsy is exactly the test that catches them.
  const empty = !value;
  const className = unread
    ? empty
      ? "font-medium text-gray-500"
      : "font-medium opacity-50"
    : "font-medium";
  return (
    <div>
      <dt className="text-xs text-gray-500">{label}</dt>
      <dd className={className} title={unread ? (empty ? NOT_READ_TITLE : STALE_TITLE) : undefined}>
        {unread && empty ? "Not reported" : (value ?? "—")}
      </dd>
    </div>
  );
}
