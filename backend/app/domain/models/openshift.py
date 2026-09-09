"""Whether a server is in use, as OpenShift reports it.

Two jobs write here and nothing else does. Every cluster reports its own
worker nodes; each MCE reports its Agents, either bound to a hosted
cluster or unbound. `lifecycle_state` is the field to read before
trusting any other: `cluster_name` is set only when something claims the
server, and `mce_id` only by the MCE job.

**Kept strictly separate from `classification.Classification`, which is a
regex verdict on a hostname.** That separation is the platform's own rule
— a naming convention is not proof of cluster membership — and it is why
`InstallationType` and `OpenShiftState` are two enums rather than one.
`InstallationType` says what *kind* of server this is; this says whether
it is in use. When they disagree the server is misnamed or misplaced, and
that disagreement is the signal. Reconciling them silently would destroy
it.

Vendor collectors never touch this: `IngestService` carries the whole
object forward untouched on every ingest, exactly as it does
`maintenance`. A server's hardware and its cluster membership are
observed by different systems on different schedules, and neither is
entitled to blank the other's findings.

**Nothing ever reports a removal.** A server freed from a cluster simply
stops appearing in that cluster's node list, so `AVAILABLE` can never be
observed directly — it is what remains when no job claims a server. Each
job therefore reconciles the set it owns (the servers naming *its*
cluster) rather than only writing what it saw; see
`app.application.services.openshift_membership`.

Correlation is by **hostname**, not MAC. Metal3 binds an Agent to a host
via `bootMACAddress`, which would be the stronger key, but it is only
available through the `BareMetalHost` — and not every server has one, so
reading it would cover part of the fleet while looking complete. The
hostname is on the Agent itself. See the module above for the two-step
read that makes it work across vendors.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from app.domain.enums import OpenShiftState


class OpenShiftLifecycle(BaseModel):
    """
    One server's observed OpenShift membership.

    Attributes:
        lifecycle_state (OpenShiftState): Whether the server is in use.
            `AVAILABLE` until a job claims it, and again once one stops.
        mce_id (str | None): The MCE that reported it. Set by the MCE job
            only; `None` on a plain cluster node, which no MCE knows about.
        cluster_name (str | None): The cluster holding it, on `INSTALLED`.
            `None` on `AVAILABLE`, and on `INSTALLED_TO_INVENTORY`, where
            an MCE holds the server but no cluster does.
        cluster_id (str | None): That cluster's own identifier where one
            is reported, for a name that is not unique across MCEs.
        role (str | None): `worker`, `master`, as the cluster reports it.
        node_name (str | None): What the cluster calls the node, which
            need not match `Server.name`.
        bmh_name (str | None): The `BareMetalHost` backing the Agent.
            Unpopulated: the jobs deliberately do not read BareMetalHosts,
            since not every server has one.
        agent_id (str | None): The `Agent` custom resource.
        boot_mac (str | None): Unpopulated, for the same reason as
            `bmh_name` — the MAC lives on the `BareMetalHost`.
        last_reported_at (datetime | None): When a job last claimed this
            server. Diagnostic rather than load-bearing: the reconcile is
            set-based, so nothing infers availability from this going
            stale.
        reported_by_agent_id (str | None): Which job instance wrote this,
            for tracing a wrong value back to the cluster that reported it.
    """

    lifecycle_state: OpenShiftState = OpenShiftState.AVAILABLE
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
