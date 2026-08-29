#!/usr/bin/env bash
# Collect one text report from an air-gapped site, to be carried back and
# read by whoever is building the collectors.
#
# Read-only by construction: it never writes to MongoDB, never runs a
# real ingest, and never sends anything anywhere. Every collector command
# below is either a `verify_*` probe or a `--dry-run`.
#
# It never prints a credential. Values whose variable name ends in
# _PASSWORD or _PEM are reported as "set (N chars)" or "empty", never
# echoed, and the report is safe to carry out of the secure environment
# once you have skimmed it. Server names and IPs ARE included, because
# they are the thing most worth looking at — treat the file accordingly.
#
# Usage, from the repository root:
#
#     scripts/field-report.sh                      # everything configured
#     scripts/field-report.sh host1 host2          # also probe these hosts
#
# Writes ./field-report-<date>.txt and prints the path.

set -uo pipefail

OUT="field-report-$(date +%Y%m%d-%H%M%S).txt"
: >"$OUT"

say() { printf '%s\n' "$*" | tee -a "$OUT"; }
run() {
  # Run a command, capturing stdout+stderr into the report.
  say ""
  say "\$ $*"
  say "---"
  "$@" >>"$OUT" 2>&1
  local rc=$?
  say "--- exit $rc"
  return 0
}
section() {
  say ""
  say "================================================================"
  say "$*"
  say "================================================================"
}

# Report whether a secret-shaped variable is set, never its value.
secret_state() {
  local name="$1" value="${!1:-}"
  if [ -z "$value" ]; then
    say "  $name = (empty)"
  else
    say "  $name = set (${#value} chars)"
  fi
}

section "0. WHERE AND WHEN"
say "date (UTC)      : $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
say "date (local)    : $(date '+%Y-%m-%d %H:%M:%S %Z')"
say "host            : $(hostname 2>/dev/null || echo unknown)"
say "git commit      : $(git rev-parse --short HEAD 2>/dev/null || echo 'not a git checkout')"
say "git branch      : $(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
# A drifted clock is indistinguishable from a bad API key to Intersight,
# so it is worth knowing before anything else fails confusingly.
if command -v timedatectl >/dev/null 2>&1; then
  say "clock sync      : $(timedatectl show -p NTPSynchronized --value 2>/dev/null || echo unknown)"
else
  say "clock sync      : timedatectl not available"
fi

section "1. WHAT IS CONFIGURED (no secret is printed)"
say "INVENTORY_INTERSIGHT_IP        = ${INVENTORY_INTERSIGHT_IP:-(empty)}"
say "INVENTORY_INTERSIGHT_API_KEY_ID= ${INVENTORY_INTERSIGHT_API_KEY_ID:-(empty)}"
secret_state INVENTORY_INTERSIGHT_API_KEY_PEM
say "INVENTORY_UCS_CENTRAL_IP       = ${INVENTORY_UCS_CENTRAL_IP:-(empty)}"
say "INVENTORY_UCS_CENTRAL_USERNAME = ${INVENTORY_UCS_CENTRAL_USERNAME:-(empty)}"
secret_state INVENTORY_UCS_CENTRAL_PASSWORD
secret_state INVENTORY_UCS_MANAGER_PASSWORD
say "INVENTORY_COLLECTOR_NAME_PATTERN = ${INVENTORY_COLLECTOR_NAME_PATTERN:-(empty)}"

section "2. IS ANY INTERSIGHT ENDPOINT REACHABLE AT ALL?"
say "An air-gapped site cannot reach intersight.com, so this must be an"
say "on-prem Intersight. A 401 below is the GOOD outcome: it means an"
say "Intersight API is listening and only the credential is missing."

probe_host() {
  local host="$1"
  say ""
  say "-- $host"
  if command -v getent >/dev/null 2>&1; then
    say "   dns  : $(getent hosts "$host" 2>/dev/null | head -1 || echo 'NO DNS RECORD')"
  fi
  # 443 only: the Intersight API is HTTPS, and a host that answers on
  # nothing else is still interesting.
  if command -v timeout >/dev/null 2>&1; then
    if timeout 5 bash -c "</dev/tcp/$host/443" 2>/dev/null; then
      say "   tcp/443: OPEN"
    else
      say "   tcp/443: closed or unreachable"
    fi
  fi
  if command -v curl >/dev/null 2>&1; then
    say "   https  : $(timeout 10 curl -sS -o /dev/null -w '%{http_code}' \
      "https://$host/api/v1/compute/PhysicalSummaries" 2>&1 | tail -1)"
    say "   (401 here is GOOD — it means an Intersight API is listening.)"
  fi
}

CANDIDATES=("$@")
if [ -n "${INVENTORY_INTERSIGHT_IP:-}" ]; then
  CANDIDATES+=("$INVENTORY_INTERSIGHT_IP")
fi
if [ ${#CANDIDATES[@]} -eq 0 ]; then
  say ""
  say "No candidate host given and INVENTORY_INTERSIGHT_IP is empty."
  say "If you believe an Intersight appliance exists here, re-run as:"
  say "    scripts/field-report.sh <its-hostname>"
else
  for host in "${CANDIDATES[@]}"; do probe_host "$host"; done
fi

if [ -n "${INVENTORY_INTERSIGHT_IP:-}" ] && [ -n "${INVENTORY_INTERSIGHT_API_KEY_ID:-}" ]; then
  section "3. INTERSIGHT — READ-ONLY PROBE"
  say "Writes nothing. Settles the TotalMemory unit and the server-name"
  say "question, which are the two things the collector cannot verify"
  say "without a live tenant."
  run uv run python -m tools.verify_intersight --show-names 15

  section "4. INTERSIGHT — DRY RUN (still writes nothing)"
  run uv run python -m tools.run_collector --manager-type INTERSIGHT --dry-run --limit 3
else
  section "3. INTERSIGHT — SKIPPED"
  say "INVENTORY_INTERSIGHT_IP / _API_KEY_ID are not both set, so the probe"
  say "that actually answers the open questions did not run. Mint a"
  say "read-only API key, export both variables plus"
  say "INVENTORY_INTERSIGHT_API_KEY_PEM, and re-run — see"
  say "docs/field-test-checklist.md."
fi

if [ -n "${INVENTORY_UCS_CENTRAL_IP:-}" ]; then
  section "5. UCS CENTRAL — READ-ONLY PROBE (worth running while you are here)"
  say "This collector already works here. The probe re-confirms it and"
  say "prints the service-profile ownership breakdown."
  run uv run python -m tools.verify_ucs_central --show-names 10

  section "6. UCS CENTRAL — DRY RUN, THREE SERVERS"
  say "The most valuable lines below are 'memory' and 'cpu': ADR-0009"
  say "could not settle whether UCS reports total memory in MB against"
  say "real hardware, and the same assumption is now carried by the"
  say "Intersight collector. Compare the memory figure against what the"
  say "server really has."
  run uv run python -m tools.run_collector --manager-type UCS_CENTRAL --dry-run --limit 3
fi

section "DONE"
say "Report written to: $OUT"
say ""
say "Before carrying this out: skim it. It contains server names, models,"
say "serial numbers and management IP addresses. It contains no password,"
say "no API key and no private key."
printf '\n%s\n' "$OUT"
