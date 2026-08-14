import { createBrowserRouter } from "react-router";

import { AppLayout } from "@/components/AppLayout";
import { ClassificationRulesPage } from "@/features/classification/ClassificationRulesPage";
import { RuleEditorPage } from "@/features/classification/RuleEditorPage";
import { HealthPoliciesPage } from "@/features/health/HealthPoliciesPage";
import { PolicyEditorPage } from "@/features/health/PolicyEditorPage";
import { InventoryPage } from "@/features/inventory/InventoryPage";
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
        path: "/classification-rules",
        element: <ClassificationRulesPage />,
      },
      {
        path: "/classification-rules/new",
        element: <RuleEditorPage />,
      },
      {
        path: "/classification-rules/:id/edit",
        element: <RuleEditorPage />,
      },
      {
        path: "/health-policies",
        element: <HealthPoliciesPage />,
      },
      {
        path: "/health-policies/new",
        element: <PolicyEditorPage />,
      },
      {
        path: "/health-policies/:id/edit",
        element: <PolicyEditorPage />,
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
