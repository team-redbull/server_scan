"""`Server` -> flat facts dict.

The one place that reaches into the nested `Server` document shape.
Everything downstream (the metric registry's resolvers, condition
evaluation) works against this flat dict, not the domain model — so a
later change to how hardware is nested on `Server` touches this one
function, not every policy evaluation path.
"""

from __future__ import annotations

from typing import Any

from app.domain.enums import HealthSeverity
from app.domain.models.server import Server


# A component reported as failed, in EITHER vocabulary this platform's
# providers speak. Drives and PSUs are each internally consistent —
# `HealthSeverity` for drives, oper-state UP/DOWN for PSUs — but GPUs are
# not: Redfish maps `Status.Health` onto `HealthSeverity` (`CRITICAL`),
# while UCS Manager, UCS Central and Intersight all map `OperState` onto
# the UP/DOWN vocabulary (`DOWN`). Both spellings mean the same thing and
# both must count, or a GPU health policy silently covers one vendor.
#
# Matching on a set of two strings rather than picking one is deliberate:
# normalizing the providers to agree would be the cleaner fix, but it
# changes what is already stored on every collected server, and this file
# has twice been the place a vocabulary mismatch turned into a check that
# counted nothing (see the dated comments below).
_FAILED = frozenset({"CRITICAL", "DOWN"})


def extract_facts(server: Server) -> dict[str, Any]:
    drive_healths = [d.health for d in server.hardware.storage.drives if d.health is not None]
    link_states = [i.link_state.value for i in server.network.interfaces]
    psu_healths = [p.health for p in server.hardware.power.psus if p.health is not None]
    gpus = server.hardware.gpus
    gpu_healths = [g.health for g in gpus if g.health is not None]
    uncorrectable = [
        g.uncorrectable_error_count for g in gpus if g.uncorrectable_error_count is not None
    ]

    return {
        "cpu.socket_count": server.hardware.cpu.sockets,
        "memory.total_bytes": server.hardware.memory.total_bytes,
        "storage.drive_count": len(server.hardware.storage.drives),
        "storage.drive_healths": drive_healths,
        # CRITICAL, not "FAILED": both collectors normalize a dead drive
        # onto `HealthSeverity` at the provider boundary, so a policy
        # counting "FAILED" counted nothing outside fake data.
        "storage.failed_drive_count": sum(
            1 for h in drive_healths if h == HealthSeverity.CRITICAL.value
        ),
        # WARNING, not CRITICAL: the degraded-but-alive drive the existing
        # `storage.failed_drive` policy deliberately does not fire on.
        # Predictive-failure lands here, which is the whole point of
        # checking it separately rather than widening that policy.
        "storage.warning_drive_count": sum(
            1 for h in drive_healths if h == HealthSeverity.WARNING.value
        ),
        "network.interface_link_states": link_states,
        "network.interface_count": len(link_states),
        # Counted UP rather than counting DOWN: a server with unused NICs
        # has DOWN links and is perfectly healthy, so "any link down" is a
        # useless signal. "Nothing is up" is the one that means something,
        # and it needs the count above beside it to tell "every link is
        # down" from "no links were reported at all".
        "network.links_up_count": sum(1 for s in link_states if s == "UP"),
        "connectivity.fabric_paths_total": server.connectivity.facts.fabric_paths_total,
        "connectivity.fabric_paths_up": server.connectivity.facts.fabric_paths_up,
        "connectivity.fabric_paths_down": server.connectivity.facts.fabric_paths_down,
        "power.psu_count": len(server.hardware.power.psus),
        # DOWN, not "not OK": no collector had ever populated `psus`
        # before 2026-09-01, so this comparison was never exercised
        # against real data. `Psu.health` uses `normalize_oper_state`'s
        # UP/DOWN/DISABLED/UNKNOWN vocabulary (the same OperState-sourced
        # pattern `Gpu.health` already uses) — "OK" is never emitted by
        # any provider, which would have counted every healthy PSU as
        # failed the moment real data arrived. UNKNOWN is deliberately
        # not counted, matching `storage.failed_drive_count` above: a
        # read failure is not evidence of a failure, and counting it as
        # one would false-alarm on every partial query.
        "power.failed_psu_count": sum(1 for h in psu_healths if h == "DOWN"),
        "gpu.count": len(gpus),
        # Both vocabularies — see `_FAILED`.
        "gpu.failed_count": sum(1 for h in gpu_healths if h in _FAILED),
        # Summed across the server's GPUs, not per card: one policy on the
        # total is what an operator wants alerting on, and the per-card
        # detail is already on the document for whoever investigates.
        # Uncorrectable ECC is the count that matters — a correctable one
        # is the mechanism working as designed, and alerting on it would
        # fire constantly on healthy hardware.
        "gpu.uncorrectable_error_count": sum(uncorrectable),
    }
