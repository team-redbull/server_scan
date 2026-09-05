"""What OpenShift observed about a server, embedded on `Server`.

Two jobs write here and nothing else does. A UPI cluster reports its own
node list; an MCE reports its Agents, each either bound to a hosted
cluster or unbound and available. `lifecycle_state` says which of those
saw the server, and is the field to read before trusting any other:
`cluster_name` means a UPI cluster on a `UPI_NODE` and a hosted cluster on
a `HOSTED_NODE`, and `mce_id` is set only by the MCE job.

**Kept strictly separate from `classification.Classification`, which is a
regex verdict on a hostname.** That separation is the platform's own rule
— a naming convention is not proof of cluster membership — and it is why
`InstallationType` and `OpenShiftState` are two enums rather than one.
When they disagree, the server is misnamed or misplaced and the two
values disagreeing is the signal. Reconciling them silently would destroy
it.

Vendor collectors never touch this: `IngestService` carries the whole
object forward untouched on every ingest, exactly as it does
`maintenance`. A server's hardware and its cluster membership are
observed by different systems on different schedules, and neither is
entitled to blank the other's findings.

`boot_mac` is the join key. Metal3 binds an Agent to a physical host via
`bootMACAddress`, so a MAC is what correlates an Agent CR back to a
server here — a stronger key than a node name, which is a DNS label an
operator can change without touching the hardware.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from app.domain.enums import OpenShiftState


class OpenShiftLifecycle(BaseModel):
    """
    One server's observed OpenShift membership.

    Attributes:
        lifecycle_state (OpenShiftState): Which job saw it and in what
            role. `UNKNOWN` until one does.
        mce_id (str | None): The MCE that reported it. Set by the MCE job
            only; `None` on a UPI node, which no MCE knows about.
        cluster_name (str | None): The UPI cluster's name on a
            `UPI_NODE`, the hosted cluster's on a `HOSTED_NODE`, `None`
            on an `AVAILABLE` Agent, which is bound to no cluster.
        cluster_id (str | None): That cluster's own identifier where one
            is reported, for a name that is not unique across MCEs.
        role (str | None): `worker`, `master`, as the cluster reports it.
        node_name (str | None): What the cluster calls the node, which
            need not match `Server.name`.
        bmh_name (str | None): The `BareMetalHost` backing the Agent.
        agent_id (str | None): The `Agent` custom resource.
        boot_mac (str | None): The MAC the Agent was correlated on.
        last_reported_at (datetime | None): When a job last saw this
            server. The only thing that can tell a live membership from
            one nobody has confirmed in weeks — nothing reports a
            *removal*, so absence is never observed, only inferred from
            this going stale.
        reported_by_agent_id (str | None): Which job instance wrote this,
            for tracing a wrong value back to the cluster that reported it.
    """

    lifecycle_state: OpenShiftState = OpenShiftState.UNKNOWN
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
