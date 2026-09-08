/**
 * The one place a timestamp's displayed timezone is decided.
 *
 * `Date#toLocaleString()` with no options renders in the *viewer's own
 * machine* timezone, which drifts the moment someone opens this UI from a
 * laptop set to UTC, or from outside Israel. Every server/event timestamp
 * this platform shows is pinned to Asia/Jerusalem instead — Israel has one
 * IANA zone covering the whole country, DST included, so there is no
 * separate "Tel Aviv" identifier to reach for.
 *
 * Deliberately does not touch how dates/numbers are *formatted* — passing
 * `undefined` as the locale keeps that on the viewer's own convention;
 * only the wall-clock time itself is forced to Israel's.
 */
const DISPLAY_TIME_ZONE = "Asia/Jerusalem";

export function formatTimestamp(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    timeZone: DISPLAY_TIME_ZONE,
    timeZoneName: "short",
  });
}
