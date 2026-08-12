import { useEffect, useState } from "react";

/**
 * Returns `value`, delayed by `delayMs` after the last change. Used to keep
 * free-text filters (like the inventory search box) from firing a network
 * request on every keystroke — hand-rolled rather than a dependency since
 * it's a five-line hook.
 */
export function useDebouncedValue<T>(value: T, delayMs: number): T {
  const [debounced, setDebounced] = useState(value);

  useEffect(() => {
    const timer = setTimeout(() => {
      setDebounced(value);
    }, delayMs);
    return () => {
      clearTimeout(timer);
    };
  }, [value, delayMs]);

  return debounced;
}
