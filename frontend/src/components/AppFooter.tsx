/** The copyright line under every page. Deliberately year-less: this
 * ships as a long-lived air-gapped image that nobody rebuilds in
 * January, so a year would simply go stale. */
export function AppFooter() {
  return (
    <footer className="mx-auto max-w-7xl px-8 pt-6 pb-10 text-base text-[var(--text-secondary)]">
      © Tomer Karniol
    </footer>
  );
}
