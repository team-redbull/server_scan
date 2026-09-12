import { Link, useLocation } from "react-router";

const LINKS = [
  { to: "/", label: "Sites" },
  { to: "/servers", label: "Servers" },
  { to: "/rules", label: "Rules & Policies" },
];

/** Minimal top-level nav for an internal admin tool — a horizontal bar,
 * no responsive hamburger menu needed. `pathname === to` (rather than
 * `startsWith`) so `/rules` doesn't also light up while on `/`, at the
 * cost of a nested route not lighting up its parent — good enough for a
 * three-link nav. */
export function AppNav() {
  const location = useLocation();

  return (
    <nav className="border-b border-[var(--border-subtle)] bg-[var(--surface-raised)]">
      <div className="mx-auto flex max-w-7xl items-center gap-6 px-8">
        {/* Three classes place it: `h-10` sizes it (width follows),
            `-ml-4` pulls it outside the container's own `px-8` so it sits
            nearer the window edge than the nav text, and the wrapper's
            `gap-6` sets the distance to "Sites". */}
        <Link to="/" className="-ml-4 flex shrink-0 items-center py-1">
          <img src="/redbull-logo.svg" alt="Red Bull" className="h-10 w-auto" />
        </Link>
        {LINKS.map((link) => {
          const isActive =
            link.to === "/" ? location.pathname === "/" : location.pathname.startsWith(link.to);
          return (
            <Link
              key={link.to}
              to={link.to}
              className={`relative py-3 text-sm font-medium ${
                isActive
                  ? "text-[var(--text-primary)]"
                  : "text-[var(--text-secondary)] hover:text-[var(--text-primary)]"
              }`}
            >
              {link.label}
              {/* A 2px bar, not just a colour change: "where am I" should
                  not depend on distinguishing two greys. */}
              {isActive && (
                <span
                  aria-hidden="true"
                  className="absolute inset-x-0 -bottom-px h-0.5 bg-[var(--color-status-info)]"
                />
              )}
            </Link>
          );
        })}
      </div>
    </nav>
  );
}
