"""Future MCE/OpenShift lifecycle state, embedded on `Server`.

Design-only in Phase 1 — no agent exists, nothing ever writes to this
except `lifecycle_state` staying `UNKNOWN` forever until a real MCE agent
lands. Kept strictly separate from `classification.Classification`
(regex-based, a naming convention) because the platform's own rule is that
regex must never be treated as proof of cluster membership.

`boot_mac` is the one field of practical near-term use even before an
agent exists: it's the join key a future MCE agent will correlate on,
since Metal3 binds an Agent to a physical host via `bootMACAddress` plus
the BMAC hostname annotation (see the existing `BareMetalHostUCS`
operator's `yaml_generators.generate_baremetal_host`).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class OpenShiftLifecycle(BaseModel):
    lifecycle_state: str = "UNKNOWN"
    mce_id: str | None = None
    cluster_name: str | None = None
    cluster_id: str | None = None
    role: str | None = None
    node_name: str | None = None
    bmh_name: str | None = None
    agent_id: str | None = None
    boot_mac: str | None = None
    last_reported_at: datetime | None = None
    reported_by_agent_id: str | None = None
