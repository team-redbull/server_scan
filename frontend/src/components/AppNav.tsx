import { Link, useLocation } from "react-router";

const LINKS = [
  { to: "/", label: "Sites" },
  { to: "/servers", label: "Servers" },
  { to: "/classification-rules", label: "Classification Rules" },
  { to: "/health-policies", label: "Health Policies" },
];

/** Minimal top-level nav for an internal admin tool — a horizontal bar,
 * no responsive hamburger menu needed. `pathname === to` (rather than
 * `startsWith`) so `/classification-rules` doesn't also light up while on
 * `/`, but a nested route like `/classification-rules/new` still doesn't
 * light up its parent — good enough for a three-link nav. */
export function AppNav() {
  const location = useLocation();

  return (
    <nav className="border-b border-[var(--border-subtle)] bg-[var(--surface-raised)]">
      <div className="mx-auto flex max-w-7xl items-center gap-6 px-8">
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
