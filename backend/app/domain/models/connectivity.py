"""Normalized fabric/connectivity model.

Not Cisco-specific despite Cisco UCS being the motivating case (fabric
interconnect A/B topology). `attachments` is a plain list of unbounded
length — nothing here assumes exactly two, because OneView, Intersight,
and non-fabric-interconnect topologies may report one, four, or zero.

This is the least-validated part of the schema: no code anywhere in the
existing UCS operator (`BareMetalHostUCS`) touches fabric interconnect
data at all (confirmed by grepping every commit in that repo's history —
only `lsServer`, `VnicEther`, `VnicIpV4PooledAddr`, `computeRackUnit`,
`mgmtInterface`, `adaptorUnit`, `adaptorHostEthIf` are ever queried). Until
a real UCS Manager collector exists, the fake data generator is the only
thing exercising this shape — treat it as the most likely part of the
schema to need revision once real UCS fabric data is seen.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class ConnectivityAttachment(BaseModel):
    type: str = "UNKNOWN"  # e.g. FABRIC_INTERCONNECT
    provider: str | None = None  # e.g. UCS_MANAGER
    fabric: str | None = None  # "A" | "B" | ... — nullable, never assumed
    fabric_name: str | None = None
    fabric_id: str | None = None
    fabric_model: str | None = None
    fabric_serial: str | None = None
    server_interface: str | None = None
    server_port: str | None = None
    fabric_port: str | None = None
    admin_state: str = "UNKNOWN"
    oper_state: str = "UNKNOWN"  # UP | DOWN | UNKNOWN
    speed_mbps: int | None = None
    last_seen: datetime | None = None


class ConnectivityFacts(BaseModel):
    """Derived, computed once at ingest and stored — health policies read
    these scalars rather than re-aggregating `attachments` on every
    evaluation. `total != up + down` is possible and deliberate: an
    attachment in an UNKNOWN or DEGRADED oper_state counts toward neither.
    """

    fabric_paths_total: int = 0
    fabric_paths_up: int = 0
    fabric_paths_down: int = 0
    fabrics_present: list[str] = Field(default_factory=list)


class Connectivity(BaseModel):
    attachments: list[ConnectivityAttachment] = Field(default_factory=list)
    facts: ConnectivityFacts = Field(default_factory=ConnectivityFacts)


def compute_connectivity_facts(attachments: list[ConnectivityAttachment]) -> ConnectivityFacts:
    """Pure function deriving `ConnectivityFacts` from a list of
    attachments. Called by the ingestion pipeline immediately after
    normalizing attachments, so the stored facts are never allowed to
    drift from the attachments they were derived from.
    """
    up = sum(1 for a in attachments if a.oper_state == "UP")
    down = sum(1 for a in attachments if a.oper_state == "DOWN")
    fabrics_present = sorted({a.fabric for a in attachments if a.fabric is not None})
    return ConnectivityFacts(
        fabric_paths_total=len(attachments),
        fabric_paths_up=up,
        fabric_paths_down=down,
        fabrics_present=fabrics_present,
    )
