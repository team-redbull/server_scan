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


def extract_facts(server: Server) -> dict[str, Any]:
    drive_healths = [d.health for d in server.hardware.storage.drives if d.health is not None]
    link_states = [i.link_state.value for i in server.network.interfaces]
    psu_healths = [p.health for p in server.hardware.power.psus if p.health is not None]

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
        "network.interface_link_states": link_states,
        "connectivity.fabric_paths_total": server.connectivity.facts.fabric_paths_total,
        "connectivity.fabric_paths_up": server.connectivity.facts.fabric_paths_up,
        "connectivity.fabric_paths_down": server.connectivity.facts.fabric_paths_down,
        "power.psu_count": len(server.hardware.power.psus),
        "power.failed_psu_count": sum(1 for h in psu_healths if h != "OK"),
    }
