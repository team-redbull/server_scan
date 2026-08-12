import { createBrowserRouter } from "react-router";

import { StatusPage } from "@/routes/StatusPage";

export const router = createBrowserRouter([
  {
    path: "/",
    element: <StatusPage />,
  },
]);
