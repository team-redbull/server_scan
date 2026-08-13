import { Outlet } from "react-router";

import { AppNav } from "@/components/AppNav";

/** Minimal shared layout: the nav bar above every route's outlet. Routes
 * themselves keep their own `<main>` wrapper (unchanged) — this only adds
 * the nav, it doesn't take over page-level layout. */
export function AppLayout() {
  return (
    <>
      <AppNav />
      <Outlet />
    </>
  );
}
