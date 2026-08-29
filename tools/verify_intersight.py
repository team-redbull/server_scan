"""Read-only probe answering the questions `docs/adr/0017` could not
settle without a live Intersight: **is the API key accepted, what does
`TotalMemory` actually mean, and does anything here name a server in a
way this platform can use?**

Those three decide whether the Intersight collector works at all. No
live Intersight call has ever been made against this code — the DevNet
sandbox went offline in August 2026 and Cisco publishes response schemas
without example values, so the mapping is built entirely from the
contract. This tool is how the first real tenant settles it, in minutes,
before a CronJob is ever scheduled.

Writes nothing: no MongoDB connection, no ingest pipeline, no `Manager`
document, no `POST` of any kind. It signs read-only `GET`s, prints a
verdict, and exits.

    uv run python -m tools.verify_intersight

Reads the same `INVENTORY_INTERSIGHT_IP`/`_USERNAME`/`_PASSWORD` the
collector does, so if this works the collector can connect too.
"""

from __future__ import annotations

import argparse
import asyncio
import re
from collections import Counter
from typing import Any

from app.config import get_settings
from app.domain.enums import ManagerType
from app.domain.ports.credentials import ManagerNotConfiguredError
from app.domain.value_objects.site import parse_site_code
from app.infrastructure.credentials import EnvConnectionResolver
from app.infrastructure.providers.intersight import mapping
from app.infrastructure.providers.intersight.client import (
    IntersightAuthError,
    IntersightClient,
    IntersightError,
    IntersightForbiddenError,
    IntersightUnreachableError,
)
from app.infrastructure.providers.intersight.signing import IntersightKeyError


def _int(value: object) -> int | None:
    """
    An integer, or None when the value is absent or not numeric.

    A local copy rather than reaching into the mapping module's private
    helper: this tool is a probe an operator runs, and it should not be
    coupled to which of the mapping's internals happen to be public.

    Args:
        value (object): Any reported value.

    Returns:
        int | None: The integer, or None.
    """
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """
    Parse the command line.

    Args:
        argv (list[str] | None): Arguments, or None for `sys.argv`.

    Returns:
        argparse.Namespace: The parsed arguments.
    """
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument(
        "--show-names",
        type=int,
        default=10,
        metavar="N",
        help="Print the first N resolved server names as a spot check (default 10, 0 to disable).",
    )
    parser.add_argument(
        "--sample",
        type=int,
        default=200,
        metavar="N",
        help="How many servers to inspect. Kept small by default; this is a probe, not a run.",
    )
    return parser.parse_args(argv)


def _p(text: str = "") -> None:
    """
    Print one line, unbuffered.

    Args:
        text (str): The line.
    """
    print(text, flush=True)


def _header(text: str) -> None:
    """
    Print a section heading.

    Args:
        text (str): The heading.
    """
    _p()
    _p(text)
    _p("-" * len(text))


async def _run(*, show_names: int, sample: int) -> int:
    """
    Probe the configured endpoint and print a verdict.

    Args:
        show_names (int): How many resolved names to print.
        sample (int): How many servers to inspect.

    Returns:
        int: 0 for GOOD, 1 for anything an operator must act on.
    """
    settings = get_settings()
    try:
        connection = EnvConnectionResolver(settings).resolve(ManagerType.INTERSIGHT)
    except ManagerNotConfiguredError as exc:
        _p(str(exc))
        return 1

    _header("1. CONNECTION")
    _p(f"endpoint : {connection.endpoint}")
    _p(f"key id   : {connection.username}")
    _p(f"key      : {'PEM supplied' if '-----BEGIN' in connection.password else 'NOT A PEM'}")

    try:
        client = IntersightClient(
            endpoint=connection.endpoint,
            key_id=connection.username,
            private_key_pem=connection.password,
            ca_bundle=settings.intersight_ca_bundle or None,
            page_size=min(sample, settings.intersight_page_size),
        )
    except (IntersightKeyError, ValueError) as exc:
        _p(f"\nFAILED before any request was sent: {exc}")
        return 1

    try:
        try:
            await client.health_check()
        except IntersightAuthError as exc:
            _p(f"\nBAD — {exc}")
            return 1
        except IntersightForbiddenError as exc:
            _p(f"\nBAD — {exc}")
            return 1
        except IntersightUnreachableError as exc:
            _p(f"\nBAD — {exc}")
            _p("      An air-gapped site cannot reach intersight.com; it needs an on-prem")
            _p("      Private Virtual Appliance, and INVENTORY_INTERSIGHT_IP set to its FQDN.")
            return 1
        _p("\nauth     : OK — the API key was accepted and inventory is readable.")

        return await _inspect(client, show_names=show_names, sample=sample)
    except IntersightError as exc:
        _p(f"\nFAILED: {exc}")
        return 1
    finally:
        await client.aclose()


async def _inspect(client: IntersightClient, *, show_names: int, sample: int) -> int:
    """
    Read a sample of servers and report what the mapping would make of it.

    Args:
        client (IntersightClient): A connected client.
        show_names (int): How many resolved names to print.
        sample (int): How many servers to inspect.

    Returns:
        int: 0 for GOOD, 1 otherwise.
    """
    settings = get_settings()

    summaries: list[dict[str, Any]] = []
    async for row in client.list_all("compute/PhysicalSummaries"):
        summaries.append(row)
        if len(summaries) >= sample:
            break

    _header("2. WHAT THIS TENANT HOLDS")
    if not summaries:
        _p("no servers at all.")
        _p()
        _p("INCONCLUSIVE — the key works and the API answers, which proves signing, paging")
        _p("               and error handling. It proves nothing about field mapping.")
        _p("               Claim some hardware and run this again.")
        return 1

    modes = Counter(mapping.management_mode(s) for s in summaries)
    _p(f"servers sampled : {len(summaries)}")
    for mode, count in modes.most_common():
        note = ""
        if mode == mapping.MODE_UCSM:
            note = "  <- owned by the UCS Central collector; not collected here by default"
        _p(f"  ManagementMode {mode:<22} {count:>6}{note}")

    collected = [
        s
        for s in summaries
        if mapping.management_mode(s) in settings.intersight_management_modes.split(",")
    ]
    _p(f"\nservers this collector would ingest: {len(collected)} of {len(summaries)}")

    profiles: dict[str, Any] = {}
    async for row in client.list_all("server/Profiles", select="Moid,Name,AssignedServer"):
        assigned = mapping.moref(row.get("AssignedServer"))
        if assigned:
            profiles[assigned] = row

    _header("3. THE SERVER NAME (the field everything else depends on)")
    _p("The platform parses a server's SITE out of its name and filters the fleet on")
    _p("INVENTORY_COLLECTOR_NAME_PATTERN, so a name sourced wrongly collects nothing.")
    _p()

    named = 0
    sited = 0
    names: list[str] = []
    for summary in collected:
        profile = profiles.get(str(summary.get("Moid") or ""))
        name = mapping.server_name(summary, profile)
        if not name:
            continue
        names.append(name)
        if profile is not None:
            named += 1
        if parse_site_code(name) is not None:
            sited += 1

    _p(f"servers with an assigned server.Profile : {named} / {len(collected)}")
    _p(f"names carrying a parseable site token   : {sited} / {len(names)}")

    pattern = settings.collector_name_pattern
    matching = [n for n in names if re.search(pattern, n)] if pattern else names
    if pattern:
        _p(f"names matching INVENTORY_COLLECTOR_NAME_PATTERN={pattern!r}: {len(matching)}")
    if show_names and names:
        _p()
        _p(f"first {min(show_names, len(names))} resolved name(s):")
        for name in names[:show_names]:
            _p(f"  {name}")

    await _check_memory_unit(client, collected)

    _header("VERDICT")
    if not matching:
        _p("BAD — no server's name matches INVENTORY_COLLECTOR_NAME_PATTERN, so a real run")
        _p("      would ingest nothing. Either the pattern is wrong for this estate, or the")
        _p("      names are not coming from where this collector looks. Check section 3.")
        return 1
    if named < len(collected):
        _p(f"PARTIAL — {len(collected) - named} server(s) have no assigned profile and fall back")
        _p("          to a label or the summary's own name. That is expected for")
        _p("          standalone-claimed servers and a problem for Intersight-managed ones.")
        _p(f"          {len(matching)} name(s) match the collection pattern, so a run would")
        _p("          collect something. Review the names above before scheduling it.")
        return 1
    _p("GOOD — every collectable server resolved a profile name, and")
    _p(f"       {len(matching)} of them match the collection pattern.")
    _p("       Run `--manager-type INTERSIGHT --dry-run` next, then check the memory note.")
    return 0


async def _check_memory_unit(client: IntersightClient, summaries: list[dict[str, Any]]) -> None:
    """
    Settle `TotalMemory`'s undocumented unit against two independent
    signals.

    ADR-0017's highest-risk open item. `TotalMemory` carries no unit
    anywhere in the contract, and the collector assumes MiB — if that is
    wrong, every server's memory is over-reported by 4.86%, silently.

    Two checks, cheapest first. `AvailableMemory` sits on the same object
    and *is* documented "in MB", so comparing the two costs no extra
    request at all. The authoritative check sums the server's DIMMs,
    whose `memory.Unit.Capacity` is documented "in MiB" — reached through
    `memory.Array`, because a `memory.Unit` carries no reference to its
    server.

    Args:
        client (IntersightClient): A connected client.
        summaries (list[dict[str, Any]]): The sampled servers.
    """
    _header("4. THE TotalMemory UNIT (ADR-0017's highest-risk open item)")
    candidate = next((s for s in summaries if _int(s.get("TotalMemory"))), None)
    if candidate is None:
        _p("no sampled server reported TotalMemory — cannot check.")
        return

    moid = str(candidate.get("Moid"))
    total = _int(candidate.get("TotalMemory")) or 0
    _p(f"server            : {candidate.get('Serial')}")
    _p(f"TotalMemory       : {total}   (no documented unit)")

    # --- signal 1: the sibling field, free.
    available = _int(candidate.get("AvailableMemory"))
    if available:
        _p(f"AvailableMemory   : {available}   (documented 'in MB')")
        if available == total:
            _p("  -> the two agree exactly. Whatever unit AvailableMemory uses,")
            _p("     TotalMemory uses it too. See the DIMM check below for which.")
        else:
            _p(f"  -> they differ (ratio {total / available:.4f}); not the same measure.")

    # --- signal 2: the DIMMs, authoritative.
    try:
        arrays = [
            array
            async for array in client.list_all(
                "memory/Arrays",
                select="Moid,ComputeBlade,ComputeRackUnit,CurrentCapacity",
                filter_expr=(f"ComputeBlade.Moid eq '{moid}' or ComputeRackUnit.Moid eq '{moid}'"),
            )
        ]
    except IntersightError as exc:
        _p(f"\ncould not read memory/Arrays: {exc}")
        _p("Filtering on a relationship's Moid may not be supported here. Compare one")
        _p("server's TotalMemory against the sum of its DIMM capacities in the Intersight")
        _p("UI by hand, and record the answer in ADR-0017 — do not skip this.")
        return

    array_moids = {str(a.get("Moid")) for a in arrays if a.get("Moid")}
    if not array_moids:
        _p("\nno memory.Array for this server — cannot reach its DIMMs.")
        return

    dimm_mib = 0
    dimm_count = 0
    try:
        for array_moid in array_moids:
            async for unit in client.list_all(
                "memory/Units",
                select="Moid,Capacity,Presence,MemoryArray",
                filter_expr=f"MemoryArray.Moid eq '{array_moid}'",
            ):
                capacity = _int(unit.get("Capacity"))
                if capacity:
                    dimm_mib += capacity
                    dimm_count += 1
    except IntersightError as exc:
        _p(f"\ncould not read memory/Units: {exc}")
        return

    _p(f"sum of DIMM sizes : {dimm_mib}   (documented MiB, across {dimm_count} DIMM(s))")
    _p()
    if not dimm_mib:
        _p("INCONCLUSIVE — no DIMM capacities came back.")
        return
    if total == dimm_mib:
        _p("SETTLED: TotalMemory is in the same unit as the DIMMs (MiB). The collector's")
        _p("         assumption is correct — record this in ADR-0017 and delete the")
        _p("         caveat, and move the fact into docs/cisco-collectors.md.")
        return
    ratio = total / dimm_mib
    _p(f"MISMATCH — TotalMemory is {ratio:.4f}x the DIMM total.")
    if abs(ratio - 1.048576) < 0.01:
        _p("           That is exactly the MB-vs-MiB ratio: TotalMemory is in decimal MB,")
        _p("           and the collector is over-reporting memory by 4.86%. Change")
        _p("           _BYTES_PER_MB to 1000 * 1000 in")
        _p("           app.infrastructure.providers.intersight.mapping BEFORE scheduling")
        _p("           the CronJob, and update ADR-0017.")
    else:
        _p("           Neither unit explains this. Do not trust memory_total_bytes until")
        _p("           it is understood; report the numbers above in ADR-0017.")


def main(argv: list[str] | None = None) -> None:
    """
    Entry point.

    Args:
        argv (list[str] | None): Arguments, or None for `sys.argv`.
    """
    args = _parse_args(argv)
    raise SystemExit(asyncio.run(_run(show_names=args.show_names, sample=args.sample)))


if __name__ == "__main__":
    main()
