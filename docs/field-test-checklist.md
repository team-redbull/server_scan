# What to run against your Intersight, and what to bring back

Written 2026-08-29, for the first real run of the Intersight collector.
Everything here is read-only: no MongoDB write, no ingest, no `POST` to
Intersight. Nothing you run can change anything in Intersight or in the
inventory database.

---

## The short version

Yes — `verify_intersight` is the one command. Four lines:

```bash
cd /path/to/server_scan

export INVENTORY_INTERSIGHT_IP=<your intersight hostname>   # bare host, no https://
export INVENTORY_INTERSIGHT_API_KEY_ID='<the API Key ID>'
export INVENTORY_INTERSIGHT_API_KEY_PEM="$(cat ~/intersight-key.pem)"

uv run python -m tools.verify_intersight --show-names 15 | tee intersight-verify.txt
```

Send back `intersight-verify.txt`. That is the whole errand, and it is
safe to run repeatedly.

If the probe passes and you want to see the actual server records it
would ingest — still writing nothing — add:

```bash
uv run python -m tools.run_collector --manager-type INTERSIGHT \
  --dry-run --limit 3 | tee intersight-dryrun.txt
```

---

## Getting the API key (it is not a username and password)

Intersight has no password login for its API at all. The credential is an
**API Key ID** plus that key's **PEM private half**.

1. In the Intersight UI: **Settings → API Keys → Generate API Key**.
2. Use an account with the **Read-Only** role — this collector only reads.
3. **Save the private key when it is shown. It is shown exactly once.**
4. Copy the **API Key ID**: a long `/`-joined string, not a username.

Either key generation works. v2 keys are RSA (`BEGIN RSA PRIVATE KEY`),
v3 are EC (`BEGIN EC PRIVATE KEY`); the collector reads the PEM header
and picks the signing algorithm itself, so there is nothing to configure.
The key must be **unencrypted** — a passphrase-protected PEM is
deliberately not supported and will say so.

`INVENTORY_INTERSIGHT_IP` is a bare hostname: no `https://`, no port, no
path. The collector builds the URL itself and rejects anything else up
front, because the `Host` it signs has to match the one it sends.

If your appliance presents a certificate from an internal CA, add:

```bash
export INVENTORY_INTERSIGHT_CA_BUNDLE=/path/to/ca-bundle.crt
```

TLS verification is never disabled — there is no flag for it. Import the
CA instead.

---

## The three things I actually need from the output

### a. The `TotalMemory` unit — the single most important line

Output section **"4. THE TotalMemory UNIT"**.

This is the highest-risk unknown in the collector. Cisco documents no
unit for `TotalMemory` anywhere — not on the summary, not on `Blade`, not
on `RackUnit`. The collector assumes MiB. **If that is wrong, every
server's memory is reported 4.86% too high, silently, forever**, and
nothing else in the platform would ever notice.

The probe settles it by summing one real server's DIMMs, whose capacity
*is* documented, and comparing. You will get one of:

- `SETTLED:` — the assumption is right. Nothing to do.
- `MISMATCH — ... MB-vs-MiB ratio` — the assumption is wrong; I change
  one constant before this is ever scheduled.
- `could not read memory/Arrays: ...` — the relationship filter isn't
  supported on your version. Then please compare by hand in the UI: one
  server's total memory against the sum of its DIMMs.

### b. Do server names come out right

Output section **"3. THE SERVER NAME"**.

The platform parses a server's **site** out of its name (`ocp4-prod-tlv-…`
→ `tlv`) and only collects servers matching `INVENTORY_COLLECTOR_NAME_PATTERN`.
Intersight's own `Name` field is a chassis slot rather than a hostname, so
the collector reads the name off the service profile instead. Set the
pattern to whatever your fleet uses before running:

```bash
export INVENTORY_COLLECTOR_NAME_PATTERN='^ocp'
```

If `names matching ...` comes back `0`, a real run would collect nothing,
and I need to know that before it is ever scheduled. The first ten
resolved names are printed so you can sanity-check them by eye.

### c. What Intersight actually manages there

Output section **"2. WHAT THIS TENANT HOLDS"**, the `ManagementMode`
counts.

- Servers in `Intersight` or `IntersightStandalone` mode are machines
  UCS Central cannot see. Those are what this collector is for.
- Servers in `UCSM` mode are deliberately **not** collected — they belong
  to the UCS Central collector, and collecting both would make one
  document's fields flip on whichever job ran last.

If it turns out to be *all* `UCSM`, this collector correctly collects
nothing at your site, and that is worth knowing plainly.

---

## If the key is rejected

Intersight answers several different problems with the same HTTP 401, but
it distinguishes them internally and the collector now reads that — so
the message tells you which of three situations you are in:

- *"the collector's request signing… not in your API key"* — a bug on my
  side, not yours. Send me the `traceId` from the message.
- *"received no API key credentials at all"* — something in transit
  stripped the header, typically a proxy.
- *"check in that order: API Key ID… private PEM… still listed"* — the
  credential itself. Also check the appliance's clock: a drifted clock
  looks exactly like a bad key, and the message says so when it can tell.

---

## Optional, and genuinely worth it while you are in there

If UCS Central is reachable from the same machine, one dry run against it
answers a question open since ADR-0009:

```bash
uv run python -m tools.run_collector --manager-type UCS_CENTRAL \
  --dry-run --limit 3 | tee ucs-dryrun.txt
```

**Compare a server's reported `memory` line against what that machine
really has.** ADR-0009 could never settle whether UCS reports total
memory in MB against real hardware — the emulator gave one synthetic
value for every model — and **the Intersight collector now carries the
same assumption**. Confirming it on real Cisco hardware settles it for
both collectors at once.

---

## What to send back, and what is in it

`intersight-verify.txt`, plus either dry-run file if you ran one.

**Skim it before it leaves the secure environment.** It deliberately
contains server names, models, serial numbers and management IP
addresses — those are the point of the exercise. It contains **no**
credential: the API key's private half is never printed, and there is no
debug flag anywhere that would print it.

If your site's rules do not allow hostnames or serials out, redact them
and say so. The memory comparison and the counts are still worth having
on their own.
