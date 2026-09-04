import type { FormEvent, ReactNode } from "react";
import { useState } from "react";

import { Badge } from "@/components/Badge";
import { HealthBadge } from "@/components/HealthBadge";
import type { ServerDetail } from "@/types/server";

interface OverviewTabProps {
  server: ServerDetail;
  /** Both omitted (rather than made required) so `OverviewTab` stays
   * usable in a read-only context — `ServerDetailPage` is the only
   * current caller and always supplies both, but nothing here should
   * force every future caller to own maintenance mutations just to
   * render an overview. */
  onEnableMaintenance?: (reason: string) => void;
  onDisableMaintenance?: () => void;
  maintenancePending?: boolean;
}

export function OverviewTab({
  server,
  onEnableMaintenance,
  onDisableMaintenance,
  maintenancePending,
}: OverviewTabProps) {
  const [reason, setReason] = useState("");

  function handleEnable(e: FormEvent) {
    e.preventDefault();
    onEnableMaintenance?.(reason);
    setReason("");
  }

  return (
    <dl className="grid grid-cols-1 gap-x-8 gap-y-4 sm:grid-cols-2">
      <Field label="Name" value={server.name} />
      <Field label="Vendor" value={server.identity?.vendor ?? "unknown"} />
      <Field label="Model" value={server.model ?? "—"} />
      <Field label="Serial" value={server.identity?.serial ?? "—"} />
      <Field label="Site" value={server.site_id ?? "—"} />
      <Field label="Manager" value={server.manager_id ?? "—"} />
      <Field label="Classification" value={<Badge>{server.classification.installation_type}</Badge>} />
      <Field label="Overall health" value={<HealthBadge severity={server.health.overall} />} />
      <Field
        label="Maintenance"
        value={
          <div className="flex flex-col gap-2">
            {server.maintenance.enabled ? (
              <>
                <Badge tone="warning">{server.maintenance.reason ?? "Enabled"}</Badge>
                {onDisableMaintenance && (
                  <button
                    type="button"
                    onClick={onDisableMaintenance}
                    disabled={maintenancePending}
                    className="w-fit rounded border border-gray-300 px-2 py-1 text-xs disabled:cursor-not-allowed disabled:opacity-40 dark:border-gray-600"
                  >
                    End maintenance
                  </button>
                )}
              </>
            ) : (
              <>
                <span>Not in maintenance</span>
                {onEnableMaintenance && (
                  <form onSubmit={handleEnable} className="flex items-center gap-2">
                    <input
                      type="text"
                      value={reason}
                      onChange={(e) => {
                        setReason(e.target.value);
                      }}
                      placeholder="Reason (optional)"
                      className="rounded border border-gray-300 px-2 py-1 text-xs dark:border-gray-600 dark:bg-gray-900"
                    />
                    <button
                      type="submit"
                      disabled={maintenancePending}
                      className="w-fit shrink-0 rounded border border-gray-300 px-2 py-1 text-xs disabled:cursor-not-allowed disabled:opacity-40 dark:border-gray-600"
                    >
                      Start maintenance
                    </button>
                  </form>
                )}
              </>
            )}
          </div>
        }
      />
      <Field
        label="Last seen"
        value={server.last_seen_at ? new Date(server.last_seen_at).toLocaleString() : "—"}
      />
      <Field label="Updated" value={new Date(server.updated_at).toLocaleString()} />
    </dl>
  );
}

function Field({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div>
      <dt className="text-xs font-medium uppercase tracking-wide text-gray-500">{label}</dt>
      <dd className="mt-1 text-sm">{value}</dd>
    </div>
  );
}
