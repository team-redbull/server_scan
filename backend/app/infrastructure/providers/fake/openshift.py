"""Fake OpenShift membership, assigned after ingest.

Separate from `..fake.generator` on purpose, and the separation is the
point rather than tidiness: a collector produces a `ProviderServer`, which
has no `openshift` field at all, because cluster membership is observed by
a different system on a different schedule. Seeding it through the
provider would model a data path that does not exist.

So this runs where the real jobs will: over servers already in the
database, reading what is there. It stands in for two CronJobs — a UPI
cluster listing its own nodes, and an MCE listing its Agents — until those
exist.

Deterministic per server rather than per run: the state is drawn from a
`Random` seeded on the server's own id, so re-running the seeder over the
same fleet produces the same answer without depending on iteration order.
"""

from __future__ import annotations

import random

from app.domain.enums import InstallationType, OpenShiftState
from app.domain.models.openshift import OpenShiftLifecycle
from app.domain.models.server import Server
from app.utils.timeutil import utcnow

# Servers no cluster and no MCE has ever reported. Not an error state — a
# machine racked but not yet handed to OpenShift looks exactly like this,
# and the UI has to render it as "nothing has said" rather than as a
# fault. The rest of the unclustered fleet is an MCE's spare pool, which
# is what `AVAILABLE` means.
_UNREPORTED_SHARE = 0.2


def _mce_for(site_id: str | None) -> str:
    """
    The MCE that manages one site.

    One per site, which is the shape an estate running a hub per location
    takes.

    Args:
        site_id (str | None): The server's site, or None.

    Returns:
        str: The MCE's name.
    """
    return f"mce-{site_id or 'unassigned'}"


def openshift_for(server: Server) -> OpenShiftLifecycle:
    """
    What OpenShift would report about one server.

    Derived from the server's own name and classification so the seeded
    result is coherent with the rest of the fleet — but *not* forced to
    agree with it. A `random-server-...` classified `UNCLASSIFIED` can
    still come back `HOSTED_NODE`, which is the disagreement the model
    exists to surface: a regex on a hostname is not proof of cluster
    membership, and a misnamed server is exactly the case worth seeing.

    Args:
        server (Server): The stored server to report on.

    Returns:
        OpenShiftLifecycle: Its membership, or an `UNKNOWN` record when
            nothing has reported it.
    """
    rng = random.Random(server.id)  # noqa: S311 - deterministic fake data
    now = utcnow()
    mce = _mce_for(server.site_id)
    draw = rng.random()

    if "hypershift" in server.name.lower() or (
        server.classification.installation_type is InstallationType.HOSTED_CLUSTER
    ):
        cluster = f"hc-{server.site_id or 'unassigned'}-{rng.randint(1, 3):02d}"
        return OpenShiftLifecycle(
            lifecycle_state=OpenShiftState.HOSTED_NODE,
            mce_id=mce,
            cluster_name=cluster,
            cluster_id=f"{cluster}-{rng.randrange(16**8):08x}",
            role=rng.choice(("worker", "worker", "master")),
            node_name=server.name,
            bmh_name=server.name,
            agent_id=f"agent-{rng.randrange(16**12):012x}",
            boot_mac=next(iter(server.identity.nic_macs), None),
            last_reported_at=now,
            reported_by_agent_id=mce,
        )

    if server.classification.installation_type is InstallationType.UPI:
        cluster = f"upi-{server.site_id or 'unassigned'}"
        return OpenShiftLifecycle(
            lifecycle_state=OpenShiftState.UPI_NODE,
            cluster_name=cluster,
            cluster_id=f"{cluster}-{rng.randrange(16**8):08x}",
            role=rng.choice(("worker", "worker", "master")),
            node_name=server.name,
            boot_mac=next(iter(server.identity.nic_macs), None),
            last_reported_at=now,
            reported_by_agent_id=cluster,
        )

    if draw < _UNREPORTED_SHARE:
        return OpenShiftLifecycle()

    # Registered to an MCE and bound to nothing. `cluster_name` stays None
    # rather than empty: there is no cluster, which is a different claim
    # from a cluster whose name went unread.
    return OpenShiftLifecycle(
        lifecycle_state=OpenShiftState.AVAILABLE,
        mce_id=mce,
        agent_id=f"agent-{rng.randrange(16**12):012x}",
        bmh_name=server.name,
        boot_mac=next(iter(server.identity.nic_macs), None),
        last_reported_at=now,
        reported_by_agent_id=mce,
    )
