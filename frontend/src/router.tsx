import { createBrowserRouter } from "react-router";

import { AppLayout } from "@/components/AppLayout";
import { InventoryPage } from "@/features/inventory/InventoryPage";
import { RulesPage } from "@/features/rules/RulesPage";
import { ServerDetailPage } from "@/features/servers/ServerDetailPage";
import { SitesOverviewPage } from "@/features/sites/SitesOverviewPage";
import { StatusPage } from "@/routes/StatusPage";

export const router = createBrowserRouter([
  {
    element: <AppLayout />,
    children: [
      {
        // The site overview is the landing page: at fleet scale a flat
        // list can't answer "is anything wrong?" without sorting and
        // scanning, and five cards can.
        path: "/",
        element: <SitesOverviewPage />,
      },
      {
        path: "/servers",
        element: <InventoryPage />,
      },
      {
        path: "/servers/:id",
        element: <ServerDetailPage />,
      },
      {
        // One page, read-only, replacing the two list pages and their four
        // editor routes. Classification and health are read together far
        // more often than either is read alone — a server's installation
        // type decides which policies even apply to it — and neither is
        // editable here on purpose: they ship with the platform so that
        // two installations classify and score identically.
        path: "/rules",
        element: <RulesPage />,
      },
      {
        // Slice 0's backend-readiness placeholder, kept as a debug page now
        // that "/" is the real inventory table.
        path: "/status",
        element: <StatusPage />,
      },
    ],
  },
]);
