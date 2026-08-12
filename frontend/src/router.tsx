import { createBrowserRouter } from "react-router";

import { InventoryPage } from "@/features/inventory/InventoryPage";
import { ServerDetailPage } from "@/features/servers/ServerDetailPage";
import { StatusPage } from "@/routes/StatusPage";

export const router = createBrowserRouter([
  {
    path: "/",
    element: <InventoryPage />,
  },
  {
    path: "/servers/:id",
    element: <ServerDetailPage />,
  },
  {
    // Slice 0's backend-readiness placeholder, kept as a debug page now
    // that "/" is the real inventory table.
    path: "/status",
    element: <StatusPage />,
  },
]);
