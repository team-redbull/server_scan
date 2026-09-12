import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";

import { ApiError } from "@/api/client";
import { useToggleMaintenanceMutation } from "@/features/inventory/hooks";
import type { ServerSummary } from "@/types/server";

/**
 * One row's maintenance switch: a crossed wrench-and-screwdriver opens a
 * card asking why, a play icon returns the server with no card at all.
 *
 * The asymmetry is deliberate and is what the operator asked for. Pausing
 * a server is the act someone else has to interpret later — in the audit
 * trail, and in the badge the inventory row shows — so it is worth a
 * sentence. Returning one to service needs no explanation and should cost
 * one click.
 *
 * Two shapes rather than one toggled colour, for the same reason
 * `SEVERITY_GLYPH` uses distinct shapes: the state has to be readable in
 * greyscale.
 *
 * Inline SVG and NOT a character — ⏸/🔧 and every other pictograph in
 * this range carries emoji presentation, so it renders as a blank box on
 * any machine with no emoji font. That is not hypothetical: it is what
 * the headless Chromium running `npm run test:e2e` does, and it is what a
 * minimal RHEL desktop does. The text glyphs this UI does use (▲ ◆ ● ›)
 * are all in fonts that ship everywhere.
 *
 * Drawn here rather than imported: an icon set would be a new dependency
 * for two shapes, and a stock asset (Flaticon and the like) carries an
 * attribution licence and, being a download, cannot reach an air-gapped
 * build at all.
 */

/** Crossed wrench and screwdriver — the conventional "maintenance" mark.
 *
 * Stroked, not filled: at 16px a filled tool silhouette collapses into a
 * blob, while an open jaw and a visible shaft still read. The two tools
 * lie on opposite diagonals so the crossing is legible at that size.
 */
function ToolsIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      className="size-4"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.9"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {/* Wrench: open jaw at top-left, shaft down to bottom-right. The
          arc is the long way round a circle at (8,8), which is what
          leaves the jaw open toward the corner. */}
      <path d="M7.7 4.8A3.2 3.2 0 1 1 4.8 7.7" />
      <path d="M10.3 10.3 19.5 19.5" />
      {/* Screwdriver on the other diagonal: blade at bottom-left, the
          handle a deliberately fatter stroke so it reads as a grip. */}
      <path d="M4.5 19.5 15.5 8.5" />
      <path d="M16.2 7.8 20 4" strokeWidth="4.2" />
    </svg>
  );
}

/** Play: back into service. */
function PlayIcon() {
  return (
    <svg viewBox="0 0 12 12" className="size-3 fill-current" aria-hidden="true">
      <path d="M3 1.5 10.5 6 3 10.5Z" />
    </svg>
  );
}

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    return error.problem.detail;
  }
  return error instanceof Error ? error.message : "Failed to update maintenance.";
}

export function MaintenanceToggle({ server }: { server: ServerSummary }) {
  const toggle = useToggleMaintenanceMutation();
  const [asking, setAsking] = useState(false);
  const [reason, setReason] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  const enabled = server.maintenance.enabled;

  // Focus the field the card exists for, so the operator can start typing
  // without reaching for the mouse a second time.
  useEffect(() => {
    if (asking) {
      inputRef.current?.focus();
    }
  }, [asking]);

  function close() {
    setAsking(false);
    setReason("");
    toggle.reset();
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    event.stopPropagation();
    const trimmed = reason.trim();
    toggle.mutate(
      { id: server.id, enable: true, ...(trimmed ? { reason: trimmed } : {}) },
      { onSuccess: close },
    );
  }

  const label = enabled
    ? `End maintenance on ${server.name}`
    : `Put ${server.name} into maintenance`;

  return (
    // The one control in a row whose click must NOT open the server, and
    // the card lives inside the cell — so the whole subtree stops
    // propagation rather than each control doing it separately.
    <span
      className="relative inline-block"
      onClick={(event) => {
        event.stopPropagation();
      }}
    >
      <button
        type="button"
        title={label}
        aria-label={label}
        aria-expanded={enabled ? undefined : asking}
        disabled={toggle.isPending}
        onClick={() => {
          if (enabled) {
            toggle.mutate({ id: server.id, enable: false });
          } else {
            setAsking(true);
          }
        }}
        className={`inline-flex size-7 items-center justify-center rounded-md border text-xs transition-colors duration-[var(--duration-instant)] ease-[var(--ease-out-strong)] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--color-status-info)] disabled:cursor-not-allowed disabled:opacity-40 ${
          enabled
            ? "border-[var(--border-strong)] bg-[var(--tint-maintenance)] text-[var(--text-on-maintenance)]"
            : "border-[var(--border-subtle)] text-[var(--text-muted)] hover:border-[var(--border-strong)] hover:text-[var(--text-primary)]"
        }`}
      >
        {enabled ? <PlayIcon /> : <ToolsIcon />}
      </button>

      {asking && (
        <>
          {/* A plain overlay rather than <dialog showModal()>: jsdom 29
              still does not implement showModal, so the native element
              cannot be tested at all. Escape and a backdrop click both
              cancel, which is the behaviour the native one would give. */}
          <span
            className="fixed inset-0 z-20 bg-black/40"
            onClick={close}
            aria-hidden="true"
          />
          <div
            role="dialog"
            aria-modal="true"
            aria-label={`Put ${server.name} into maintenance`}
            onKeyDown={(event) => {
              if (event.key === "Escape") close();
            }}
            className="absolute top-full right-0 z-30 mt-1 w-80 rounded-[var(--radius-card)] border border-[var(--border-strong)] bg-[var(--surface-raised)] p-4 text-left shadow-lg"
          >
            <h2 className="text-sm font-semibold text-[var(--text-primary)]">
              Put into maintenance
            </h2>
            <p className="mt-0.5 truncate text-xs text-[var(--text-muted)]" title={server.name}>
              {server.name}
            </p>

            <form onSubmit={submit} className="mt-3">
              <label
                htmlFor={`maint-reason-${server.id}`}
                className="block text-xs text-[var(--text-secondary)]"
              >
                Why is it going into maintenance?
              </label>
              <input
                id={`maint-reason-${server.id}`}
                ref={inputRef}
                type="text"
                value={reason}
                onChange={(event) => {
                  setReason(event.target.value);
                }}
                placeholder="Replacing PSU 2 — INC-4417"
                className="mt-1.5 w-full rounded-md border border-[var(--border-subtle)] bg-[var(--surface-sunken)] px-2 py-1.5 text-sm text-[var(--text-primary)] placeholder:text-[var(--text-muted)] focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-[var(--color-status-info)]"
              />

              {toggle.isError && (
                <p role="alert" className="mt-2 text-xs text-[var(--text-on-critical)]">
                  {errorMessage(toggle.error)}
                </p>
              )}

              <div className="mt-3 flex justify-end gap-2">
                <button
                  type="button"
                  onClick={close}
                  className="rounded-md border border-[var(--border-subtle)] px-2.5 py-1 text-xs text-[var(--text-secondary)] hover:border-[var(--border-strong)] hover:text-[var(--text-primary)]"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={toggle.isPending}
                  className="rounded-md border border-[var(--border-strong)] bg-[var(--tint-maintenance)] px-2.5 py-1 text-xs font-medium text-[var(--text-on-maintenance)] disabled:cursor-not-allowed disabled:opacity-40"
                >
                  {toggle.isPending ? "Starting…" : "Start maintenance"}
                </button>
              </div>
            </form>
          </div>
        </>
      )}
    </span>
  );
}
