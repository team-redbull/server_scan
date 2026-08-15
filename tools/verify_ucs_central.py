"""Read-only probe answering the one question `docs/adr/0014` could not
settle without a live UCS Central: **does Central's `lsServer` include
each domain's locally-defined service profiles, or only the global ones
Central itself owns?**

That question decides whether the UCS Central collector works at all. A
UCS server's name comes from its service profile — `computeBlade.name` is
empty in practice (`docs/adr/0009`) — and the name is what carries the
site token, the classification pattern, and the
`INVENTORY_COLLECTOR_NAME_PATTERN` match. If Central holds only global
profiles, every server under a local profile arrives named after its
chassis slot and is silently dropped.

Writes nothing: no MongoDB connection, no ingest pipeline, no `Manager`
document. It logs in, runs read-only queries, prints a verdict, logs out.

    uv run python -m tools.verify_ucs_central

Reads the same `INVENTORY_UCS_CENTRAL_IP`/`_USERNAME`/`_PASSWORD` the
collector does, so if this works the collector can connect too.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
from collections import Counter

from app.config import get_settings
from app.domain.enums import ManagerType
from app.domain.ports.credentials import ManagerNotConfiguredError
from app.infrastructure.credentials import EnvConnectionResolver
from app.infrastructure.providers.ucs_central.client import UcsCentralClient
from app.infrastructure.providers.ucs_central.provider import domain_id_from_dn
from app.infrastructure.providers.ucs_common import TEMPLATE_TYPES, is_equipped


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
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
    return parser.parse_args(argv)


def _p(text: str = "") -> None:
    print(text, flush=True)


def _header(text: str) -> None:
    _p()
    _p(text)
    _p("=" * len(text))


async def _run(show_names: int) -> int:
    settings = get_settings()
    try:
        connection = EnvConnectionResolver(settings).resolve(ManagerType.UCS_CENTRAL)
    except ManagerNotConfiguredError as exc:
        _p(str(exc))
        return 2

    client = UcsCentralClient(
        endpoint=connection.endpoint,
        username=connection.username,
        password=connection.password,
        timeout_seconds=max(settings.collector_connect_timeout_seconds, 60.0),
    )

    _p(f"Connecting to UCS Central at {connection.endpoint} as {connection.username} ...")
    await client.login()
    try:
        domains = await client.query_classid("computeSystem")
        blades = await client.query_classid("computeBlade")
        racks = await client.query_classid("computeRackUnit")
        ls_servers = await client.query_classid("lsServer")
        # The decisive query. `lsSPMeta` hangs off `lsServer` and carries
        # `ownership_state`, whose value set is
        # ['delete-pending', 'disassoc-pending', 'global-controlled',
        #  'localized'] — "localized" meaning a profile owned by its own
        # domain rather than by Central.
        sp_meta = await client.query_classid("lsSPMeta")
        # Central's own per-domain inventory sync state.
        inventory_eps = await client.query_classid("inventoryDomainEp")
    finally:
        await client.logout()

    servers = [mo for mo in (*blades, *racks) if is_equipped(mo)]
    profiles = {
        mo.dn: mo for mo in ls_servers if str(getattr(mo, "type", "") or "") not in TEMPLATE_TYPES
    }
    templates = [mo for mo in ls_servers if str(getattr(mo, "type", "") or "") in TEMPLATE_TYPES]

    _header("1. Registered domains")
    sync_by_sys = {str(getattr(m, "sys_id", "") or ""): m for m in inventory_eps}
    _p(f"{'ID':<8} {'NAME':<26} {'ADDRESS':<16} {'INV STATUS':<14} {'REPORTED':>8} {'SEEN':>6}")
    seen_by_domain = Counter(
        did for mo in servers if (did := domain_id_from_dn(getattr(mo, "dn", ""))) is not None
    )
    for d in sorted(domains, key=lambda m: str(getattr(m, "name", ""))):
        did = str(getattr(d, "id", "") or "")
        _p(
            f"{did:<8} "
            f"{getattr(d, 'name', '') or '—'!s:<26} "
            f"{getattr(d, 'address', '') or '—'!s:<16} "
            f"{getattr(d, 'inventory_status', '') or '—'!s:<14} "
            f"{getattr(d, 'total_physical_cnt', '') or '?'!s:>8} "
            f"{seen_by_domain.get(did, 0):>6}"
        )
        sync = sync_by_sys.get(did)
        if sync is not None:
            _p(f"{'':<8} last inventory update: {getattr(sync, 'latest_update_time', '—')}")
    _p()
    _p(f"{len(domains)} domain(s), {len(servers)} equipped server(s) collected across all of them.")

    _header("2. THE DECISIVE QUESTION — are local service profiles present?")
    ownership = Counter(str(getattr(m, "ownership_state", "") or "?") for m in sp_meta)
    _p(
        f"lsServer objects returned : {len(ls_servers)}  "
        f"({len(profiles)} profiles, {len(templates)} templates)"
    )
    _p(f"lsSPMeta objects returned : {len(sp_meta)}")
    _p()
    if ownership:
        _p("ownership_state breakdown:")
        for state, count in ownership.most_common():
            note = {
                "localized": "  <-- owned by its own domain (a LOCAL profile)",
                "global-controlled": "  <-- owned by UCS Central (a GLOBAL profile)",
            }.get(state, "")
            _p(f"  {state:<22} {count:>6}{note}")
    else:
        _p("No lsSPMeta objects returned at all.")

    _header("3. Do servers actually resolve a name?")
    resolved = 0
    unresolved_by_domain: Counter[str] = Counter()
    names: list[str] = []
    for mo in servers:
        profile = profiles.get(getattr(mo, "assigned_to_dn", None) or "")
        name = getattr(profile, "name", None) if profile is not None else None
        if name:
            resolved += 1
            names.append(str(name))
        else:
            unresolved_by_domain[domain_id_from_dn(getattr(mo, "dn", "")) or "?"] += 1

    _p(f"servers with a resolved service-profile name : {resolved} / {len(servers)}")
    if unresolved_by_domain:
        _p("servers with NO name, by domain:")
        for did, count in unresolved_by_domain.most_common():
            _p(f"  domain {did:<10} {count:>6}")

    pattern = settings.collector_name_pattern
    if pattern:
        matching = [n for n in names if re.search(pattern, n)]
        _p()
        _p(f"names matching INVENTORY_COLLECTOR_NAME_PATTERN={pattern!r}: {len(matching)}")
    if show_names and names:
        _p()
        _p(f"first {min(show_names, len(names))} resolved name(s):")
        for n in names[:show_names]:
            _p(f"  {n}")

    _header("VERDICT")
    localized = ownership.get("localized", 0)
    if not servers:
        _p("INCONCLUSIVE — Central returned no equipped servers at all. Check that domains are")
        _p("registered and their inventory has synced (section 1).")
        return 1
    if resolved == len(servers):
        _p("GOOD — every collected server resolved a service-profile name.")
        if localized:
            _p(f"       {localized} profile(s) are 'localized', so Central DOES replicate")
            _p("       domain-local service profiles. ADR-0014's open question is settled: yes.")
        else:
            _p("       Note: no 'localized' profiles exist here, so this fleet does not exercise")
            _p("       the local-profile case. It works, but the question stays open for a fleet")
            _p("       that does use local profiles.")
        _p("       The UCS Central collector is safe to run for this fleet.")
        return 0
    if resolved == 0:
        _p("BAD — no server resolved a name. Every one would fall back to a chassis-slot DN,")
        _p("      carry no site token, and be dropped by INVENTORY_COLLECTOR_NAME_PATTERN.")
        _p("      Collect these domains through their own UCS Manager instead.")
        return 1
    _p(f"PARTIAL — {resolved} of {len(servers)} servers resolved a name.")
    _p("          The domains listed in section 3 would lose their servers entirely.")
    _p("          Collect those domains through their own UCS Manager.")
    return 1


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    # Shares the collector's XML dump switch, for when a result needs
    # explaining rather than just reporting.
    if os.environ.get("INVENTORY_UCS_DUMP_XML") == "1":
        _p("(INVENTORY_UCS_DUMP_XML=1 — raw XML will be dumped)")
    raise SystemExit(asyncio.run(_run(args.show_names)))


if __name__ == "__main__":
    main()
