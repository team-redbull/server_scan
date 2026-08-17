# Testing the UCS collector against a real environment

A step-by-step runbook for proving the Cisco collector actually reaches
your hardware and returns the servers you expect — in an air-gapped
environment, without writing anything until you choose to.

The steps are ordered so the **cheapest failure comes first**: step 1
needs nothing but network access to UCS Central, step 2 adds MongoDB and
logs into every domain, and only step 3 writes. If step 1 fails there is
no point running step 2.

There is exactly one Cisco collector — `--manager-type UCS_CENTRAL`. It
uses UCS Central to discover which domains exist and what service
profiles they hold, then reads each domain's real inventory from that
domain's own UCS Manager. `--manager-type UCS_MANAGER` is not a runnable
collector and will tell you so. See `cisco-collectors.md` for the
mechanics and `adr/0014-ucs-central-multi-domain-collector.md` for why.

## 0. Configuration

Five values, set in `.env` or exported. Note there is **no**
`INVENTORY_UCS_MANAGER_IP` — Central supplies every domain's address, so
only the UCS Manager *login* is configured.

```bash
export INVENTORY_UCS_CENTRAL_IP=ucsc.example.com      # bare host or IP
export INVENTORY_UCS_CENTRAL_USERNAME=inventory-svc
export INVENTORY_UCS_CENTRAL_PASSWORD='...'
export INVENTORY_UCS_MANAGER_USERNAME=inventory-svc
export INVENTORY_UCS_MANAGER_PASSWORD='...'
export INVENTORY_COLLECTOR_NAME_PATTERN='^ocp'
```

Two things that will waste your morning if you get them wrong:

- **The IP must be a bare hostname or address.** Not `https://ucsc...`,
  not `ucsc...:443`. Both SDKs build the URL themselves, so a scheme
  produces `https://https://host:443`. The collector rejects both up
  front rather than failing later as an opaque connection error.
- **The UCS Manager account must authenticate against *every* registered
  domain.** It is one service account used many times, not one per
  domain. A domain that rejects it is skipped with a logged error and the
  run still exits 0 — see "The failure that does not announce itself".

Optional: `INVENTORY_UCS_CENTRAL_DOMAIN_CONCURRENCY` (default `4`) caps
how many domains are read at once.

## 1. Read-only probe — no database required

```bash
uv run python -m tools.verify_ucs_central --show-names 20
```

Start here. It talks only to UCS Central, needs **no MongoDB**, writes
nothing, and answers the one question the whole design depends on:
does Central list the service profiles that name your servers?

It prints registered domains with their sync state, the `ownership_state`
breakdown across every `lsSPMeta` (a non-zero `localized` count means
Central does hold domain-local profiles), how many servers resolve a
name, how many match your name pattern, and a verdict:

| Verdict | Meaning |
|---|---|
| `GOOD` | every collected server resolved a service-profile name |
| `PARTIAL` | some did — the message says how many of how many |
| `BAD` | none did; every server would fall back to a chassis-slot DN, carry no site token, and fail the name filter |

`--show-names N` prints the first N resolved names as a spot check
(default 10, `0` to disable). Read them: they are what site parsing,
classification and `INVENTORY_COLLECTOR_NAME_PATTERN` all key off.

**What this step cannot tell you:** it never logs into a UCS Manager, so
it says nothing about whether your UCSM credentials work per domain, and
nothing about the hardware detail the collector actually ingests. That is
step 2.

If this reports `BAD` or `PARTIAL`, record the result in
`adr/0014-ucs-central-multi-domain-collector.md` — its "What is still
unproven" section is waiting on exactly this answer.

## 2. Dry run — the real path, still writing nothing

```bash
uv run python -m tools.run_collector --manager-type UCS_CENTRAL --dry-run --limit 5
```

This is the first command that logs into each domain's UCS Manager. It
prints the `ProviderServer` each domain reports — identity, CPU, memory,
storage drives, BMC address, NIC MACs, fabric attachments, and which site
each name resolves to — *before* the ingestion pipeline reshapes any of
it. Nothing is classified, health-evaluated, audited or upserted.

> **`--dry-run` still needs MongoDB reachable.** It writes nothing, but
> the process connects and issues a `ping` before the dry-run branch runs,
> so an unreachable database fails the command at startup. That failure is
> not a UCS problem — check `INVENTORY_MONGO_URI` before suspecting the
> collector.

`--limit N` stops after N servers per run, which is what makes this cheap
to iterate on. To see the raw XML on the wire, add `--debug-xml` — pair it
with `--limit 1`, it is extremely verbose. Passwords are masked by the SDK.

Drop `--limit` once it looks right, to see the full set the run would
ingest.

## 3. The real run

```bash
uv run python -m tools.run_collector --manager-type UCS_CENTRAL
```

Classify, health-evaluate, audit and upsert — one write per server. It
prints a summary line:

```
manager=ucs-central fetched=412 created=412 updated=0 errors=0
```

Exit codes: `0` success, `1` the run failed (see logs), `2` configuration
is missing — and a `2` names the exact environment variables to set
rather than making you guess.

### In-cluster

The collector runs as a CronJob. To trigger one immediately rather than
waiting for its schedule:

```bash
kubectl create job --from=cronjob/<release>-collector-ucs-central ucs-manual-1
kubectl logs -f job/ucs-manual-1
```

Replace `<release>` with your Helm release name. Delete the job when done
(`kubectl delete job ucs-manual-1`) so a repeat run can reuse the name.

## 4. Reading the logs

Structured JSON, one event per line. The ones that matter:

| Event | What it tells you |
|---|---|
| `ucs_central.domain_plan` | how many domains will be collected vs skipped |
| `ucs_central.domain_summary` | per domain: `total_physical_cnt` (what Central claims it holds) against `collected_servers` (what its UCS Manager actually returned). `None` means never contacted, `0` means contacted and empty — these are different problems |
| `ucs_central.domain_collected` | one line per domain that succeeded, with its server count |
| `ucs_central.domain_failed` | a domain that errored — login rejected, unreachable, timed out |
| `ucs_central.domain_without_address` | a registered domain with no address to connect to |
| `ucs_central.domain_collected_nothing` | Central reported servers, its UCS Manager returned none |
| `collector.name_filter_applied` | `kept` and `skipped` counts for `INVENTORY_COLLECTOR_NAME_PATTERN` |

Interpreting the name filter, which is the usual source of an
unexpectedly empty inventory:

- `kept=0 skipped=900` — you reached the fleet; your pattern is wrong.
- `kept=0 skipped=0` — you reached nothing. Wrong endpoint, or every
  domain failed.

## The failure that does not announce itself

**A domain that fails is logged and skipped, and the run still exits 0.**
This is deliberate — one unreachable domain must not cost the other nine
their collection — but it means a wrong password on a single domain looks
exactly like a healthy run with a quietly smaller fleet.

Before trusting a result, check two things:

```bash
# every domain that succeeded
grep ucs_central.domain_collected <logfile> | wc -l

# every domain that did not
grep ucs_central.domain_failed <logfile>
```

The first count should equal the number of registered domains you expect.
`domain_failed` should be empty.

A related quiet case: a domain is **skipped** (never contacted) when
Central lists profiles for it and none match your name pattern. That is
correct behaviour, not a fault — but if a domain you expected is missing
from `domain_collected`, check `domain_plan` to see whether it was skipped
rather than failed. A domain for which Central lists *no* profiles at all
is always collected, never skipped, precisely so an incomplete replica
cannot silently shrink your fleet.

## 5. Confirming the data landed

```bash
curl -s localhost:8080/api/v1/servers | head
curl -s 'localhost:8080/api/v1/servers?site_id=one' | head
```

Or open the UI, whose landing page is a per-site overview — a site showing
"Unassigned" servers means those names carried no recognisable site token,
which is a naming problem rather than a collector one.

## Air-gapped notes

Nothing here reaches the internet at runtime. Both Cisco SDKs
(`ucsmsdk`, `ucscsdk`) are ordinary Python packages installed from your
mirror; see `air-gap.md` for mirroring `requirements.txt` / `pylock.toml`
and the container base images. In-cluster the collector runs the same
image as the API — `tools/` is copied in alongside `app/` specifically so
no second image is needed.

The collector makes outbound HTTPS (443) connections to UCS Central and
to **every** registered UCS Manager. If your egress rules are per-host,
Central alone is not enough; each domain's Fabric Interconnect address
needs to be reachable from the collector pod too. Step 2 is what proves
that, and `ucs_central.domain_failed` is what a missing rule looks like.

## Related documents

- `cisco-collectors.md` — the verified implementation facts behind the collector
- `adr/0014-ucs-central-multi-domain-collector.md` — why Central drives it, and what is still unproven
- `adr/0009-ucs-manager-collector.md` — the UCS Manager data path and its UCS Platform Emulator validation
- `air-gap.md` — dependency mirroring
