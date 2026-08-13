import { Link, useLocation } from "react-router";

const LINKS = [
  { to: "/", label: "Inventory" },
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
    <nav className="border-b border-gray-200 bg-white dark:border-gray-700 dark:bg-gray-950">
      <div className="mx-auto flex max-w-7xl items-center gap-6 px-8 py-3">
        {LINKS.map((link) => {
          const isActive =
            link.to === "/" ? location.pathname === "/" : location.pathname.startsWith(link.to);
          return (
            <Link
              key={link.to}
              to={link.to}
              className={`text-sm font-medium ${
                isActive
                  ? "text-blue-600 dark:text-blue-400"
                  : "text-gray-600 hover:text-gray-900 dark:text-gray-400 dark:hover:text-gray-100"
              }`}
            >
              {link.label}
            </Link>
          );
        })}
      </div>
    </nav>
  );
}
