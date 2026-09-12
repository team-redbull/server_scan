import { Outlet } from "react-router";

import { AppFooter } from "@/components/AppFooter";
import { AppNav } from "@/components/AppNav";

/** Minimal shared layout: the nav above every route's outlet and the
 * copyright below it. Routes keep their own `<main>` wrapper — this does
 * not take over page-level layout.
 *
 * `min-h-dvh` + `flex-1` on the outlet's wrapper, so the footer sits at
 * the bottom of the viewport on a short page instead of floating up
 * under the last row, and is pushed down normally on a long one. */
export function AppLayout() {
  return (
    <div className="flex min-h-dvh flex-col">
      <AppNav />
      <div className="flex-1">
        <Outlet />
      </div>
      <AppFooter />
    </div>
  );
}
