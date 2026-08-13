import { useState } from "react";
import { Link, useParams } from "react-router";

import { ApiError } from "@/api/client";
import { ConnectivityTab } from "@/features/servers/ConnectivityTab";
import { HardwareTab } from "@/features/servers/HardwareTab";
import {
  useDisableMaintenanceMutation,
  useEnableMaintenanceMutation,
  useServerDetailQuery,
} from "@/features/servers/hooks";
import { NetworkTab } from "@/features/servers/NetworkTab";
import { OverviewTab } from "@/features/servers/OverviewTab";

const TABS = ["overview", "hardware", "network", "connectivity"] as const;
type TabId = (typeof TABS)[number];

const TAB_LABELS: Record<TabId, string> = {
  overview: "Overview",
  hardware: "Hardware",
  network: "Network",
  connectivity: "Connectivity",
};

export function ServerDetailPage() {
  const { id } = useParams<{ id: string }>();
  const [activeTab, setActiveTab] = useState<TabId>("overview");

  const { data, isPending, isError, error } = useServerDetailQuery(id ?? "");
  const enableMaintenanceMutation = useEnableMaintenanceMutation(id ?? "");
  const disableMaintenanceMutation = useDisableMaintenanceMutation(id ?? "");

  return (
    <main className="mx-auto max-w-5xl p-8">
      <Link to="/" className="text-sm text-blue-600 hover:underline dark:text-blue-400">
        ← Back to inventory
      </Link>

      {isPending && <p className="mt-4 text-gray-500">Loading server…</p>}

      {isError && (
        <p className="mt-4 rounded border border-red-300 bg-red-50 p-3 text-red-700 dark:border-red-800 dark:bg-red-950 dark:text-red-300">
          {error instanceof ApiError
            ? error.problem.detail
            : error instanceof Error
              ? error.message
              : "Failed to load server."}
        </p>
      )}

      {data && (
        <>
          <h1 className="mt-4 text-2xl font-semibold">{data.name}</h1>
          <p className="text-sm text-gray-500">{data.model}</p>

          <div className="mt-6 border-b border-gray-200 dark:border-gray-700">
            <nav className="-mb-px flex gap-4" aria-label="Server detail tabs">
              {TABS.map((tab) => (
                <button
                  key={tab}
                  type="button"
                  onClick={() => {
                    setActiveTab(tab);
                  }}
                  aria-current={activeTab === tab ? "page" : undefined}
                  className={`border-b-2 px-1 py-2 text-sm font-medium ${
                    activeTab === tab
                      ? "border-blue-600 text-blue-600 dark:border-blue-400 dark:text-blue-400"
                      : "border-transparent text-gray-500 hover:text-gray-700 dark:hover:text-gray-300"
                  }`}
                >
                  {TAB_LABELS[tab]}
                </button>
              ))}
            </nav>
          </div>

          <div className="mt-6">
            {activeTab === "overview" && (
              <OverviewTab
                server={data}
                onEnableMaintenance={(reason) => {
                  enableMaintenanceMutation.mutate(reason ? { reason } : {});
                }}
                onDisableMaintenance={() => {
                  disableMaintenanceMutation.mutate();
                }}
                maintenancePending={
                  enableMaintenanceMutation.isPending || disableMaintenanceMutation.isPending
                }
              />
            )}
            {activeTab === "hardware" && <HardwareTab hardware={data.hardware} />}
            {activeTab === "network" && <NetworkTab network={data.network} />}
            {activeTab === "connectivity" && <ConnectivityTab connectivity={data.connectivity} />}
          </div>
        </>
      )}
    </main>
  );
}
