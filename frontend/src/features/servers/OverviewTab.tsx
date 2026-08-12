import type { ReactNode } from "react";

import { Badge } from "@/components/Badge";
import { HealthBadge } from "@/components/HealthBadge";
import type { ServerDetail } from "@/types/server";

export function OverviewTab({ server }: { server: ServerDetail }) {
  return (
    <dl className="grid grid-cols-1 gap-x-8 gap-y-4 sm:grid-cols-2">
      <Field label="Name" value={server.name} />
      <Field label="Vendor" value={server.identity?.vendor ?? "unknown"} />
      <Field label="Model" value={server.model} />
      <Field label="Serial" value={server.identity?.serial ?? "—"} />
      <Field label="Site" value={server.site_id} />
      <Field label="Manager" value={server.manager_id} />
      <Field label="Classification" value={<Badge>{server.classification.installation_type}</Badge>} />
      <Field label="Overall health" value={<HealthBadge severity={server.health.overall} />} />
      <Field
        label="Maintenance"
        value={
          server.maintenance.enabled ? (
            <Badge tone="warning">{server.maintenance.reason ?? "Enabled"}</Badge>
          ) : (
            "Not in maintenance"
          )
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
