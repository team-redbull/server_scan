import type { ReactNode } from "react";

/** A field the last collection could not read, with nothing stored from an
 * earlier one: the `0`/`[]` below it is the model's zero, not a reading. */
export const NOT_READ_TITLE = "The most recent collection could not read this.";

/** Read on an earlier run and carried forward — real data, just not
 * confirmed by the latest collection. Shown, marked, never hidden. */
export const STALE_TITLE = "Not confirmed by the most recent collection.";

/**
 * A visible "unconfirmed" marker for carried-forward data.
 *
 * Real text rather than a `title` tooltip or opacity alone, so a keyboard
 * or screen-reader user gets the same fact a sighted mouse user gets from
 * hovering — the one mechanism this codebase has to stop a confident zero
 * used to be conveyed only through channels those users cannot reach.
 */
export function UnconfirmedMarker() {
  return (
    <span className="ml-1.5 align-middle text-[10px] font-medium tracking-wide text-amber-600 uppercase dark:text-amber-400">
      unconfirmed
    </span>
  );
}

/**
 * Renders a block honestly when the most recent collection could not read
 * it: "Not reported" in place of the zero it would otherwise state as
 * fact, or the carried-forward value dimmed and marked "unconfirmed" when
 * there is one. A field that was read renders its children untouched.
 */
export function Reported({
  unread,
  empty,
  inline = false,
  children,
}: {
  unread: boolean;
  /** Whether the stored value is the model's zero (`0`, `[]`) — the case
   * where showing it at all would be a claim no collector ever made. */
  empty: boolean;
  /** Render inside a line of text rather than as its own block. */
  inline?: boolean;
  children: ReactNode;
}) {
  if (!unread) {
    return <>{children}</>;
  }
  if (empty) {
    return inline ? (
      <span className="text-gray-500" title={NOT_READ_TITLE}>
        Not reported
      </span>
    ) : (
      <p className="mt-2 text-sm text-gray-500" title={NOT_READ_TITLE}>
        Not reported
      </p>
    );
  }
  const Tag = inline ? "span" : "div";
  return (
    <Tag className="opacity-70" title={STALE_TITLE}>
      {children}
      <UnconfirmedMarker />
    </Tag>
  );
}
