"""One observation of a server, from a cluster's own point of view.

Pure data and pure functions: `client.py` makes every API call and hands
this module plain dicts. The hostname rules live here because they are the
whole reason this design works across vendors, and they are worth testing
without a cluster.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.domain.enums import OpenShiftState


def clean_hostname(raw: object) -> str | None:
    """
    Reduce a reported hostname to the form server names are stored in.

    Args:
        raw (object): A hostname as a cluster reported it, in any shape.

    Returns:
        str | None: Lowercased, whitespace-stripped, with any trailing DNS
            domain removed. `None` for anything empty — never `""`, which
            is the distinction the two-step read in `agent_observation`
            depends on.
    """
    if not isinstance(raw, str):
        return None
    host = raw.strip().lower().split(".", 1)[0]
    return host or None


@dataclass(frozen=True, slots=True)
class ClusterObservation:
    """
    What one cluster reports about one machine.

    `hostname` is the match key and is never stored; everything else is
    written onto `Server.openshift` when the hostname resolves to a server.

    Attributes:
        hostname (str): The cleaned hostname to correlate on.
        lifecycle_state (OpenShiftState): What this observation claims.
        cluster_name (str | None): The cluster holding it, if any.
        mce_id (str | None): The reporting MCE, on the agents path only.
        node_name (str | None): What the cluster calls the node.
        role (str | None): The node's role, where reported.
        agent_id (str | None): The `Agent` resource, on the agents path.
    """

    hostname: str
    lifecycle_state: OpenShiftState
    cluster_name: str | None = None
    mce_id: str | None = None
    node_name: str | None = None
    role: str | None = None
    agent_id: str | None = None


def node_observation(node: dict[str, Any], *, cluster_name: str) -> ClusterObservation | None:
    """
    Read one `Node` as an observation.

    Args:
        node (dict[str, Any]): A `Node` resource.
        cluster_name (str): The cluster this job runs in.

    Returns:
        ClusterObservation | None: The observation, or `None` for a node
            with no usable name, which is counted as unmatched rather than
            guessed at.
    """
    metadata = node.get("metadata") or {}
    hostname = clean_hostname(metadata.get("name"))
    if hostname is None:
        return None
    labels = metadata.get("labels") or {}
    return ClusterObservation(
        hostname=hostname,
        lifecycle_state=OpenShiftState.INSTALLED,
        cluster_name=cluster_name,
        node_name=str(metadata.get("name")),
        role="master" if "node-role.kubernetes.io/master" in labels else "worker",
    )


def agent_observation(agent: dict[str, Any], *, mce_id: str) -> ClusterObservation | None:
    """
    Read one `Agent` as an observation.

    The hostname is read in two steps, and the order is the whole point.
    Vendors disagree about what a host calls itself: on Cisco the reported
    hostname *is* the server's name, while on Dell it is derived from a MAC
    and matches nothing — there, the server's name is only in the
    **requested** hostname an operator set. So the requested one wins, and
    the reported one is the fallback.

    Args:
        agent (dict[str, Any]): An `Agent` custom resource.
        mce_id (str): The MCE this job runs in.

    Returns:
        ClusterObservation | None: `INSTALLED` with the cluster when the
            Agent is bound to one, `INSTALLED_TO_INVENTORY` when it is not.
            `None` when neither hostname is usable.
    """
    spec = agent.get("spec") or {}
    inventory = (agent.get("status") or {}).get("inventory") or {}

    # `clean_hostname` returns None rather than "" for a blank value, so a
    # present-but-empty `spec.hostname` falls through to the reported one
    # instead of short-circuiting this `or` — which is exactly the Dell
    # case this two-step read exists for.
    hostname = clean_hostname(spec.get("hostname")) or clean_hostname(inventory.get("hostname"))
    if hostname is None:
        return None

    cluster = spec.get("clusterDeploymentName") or {}
    cluster_name = cluster.get("name") if isinstance(cluster, dict) else None
    agent_id = (agent.get("metadata") or {}).get("name")

    if cluster_name:
        return ClusterObservation(
            hostname=hostname,
            lifecycle_state=OpenShiftState.INSTALLED,
            cluster_name=str(cluster_name),
            mce_id=mce_id,
            node_name=str(spec.get("hostname") or inventory.get("hostname") or ""),
            role=str(spec.get("role")) if spec.get("role") else None,
            agent_id=str(agent_id) if agent_id else None,
        )

    # Registered to the MCE, bound to nothing: the spare pool cluster
    # creation draws on. `cluster_name` stays None — there is no cluster,
    # which is a different claim from a cluster whose name went unread.
    return ClusterObservation(
        hostname=hostname,
        lifecycle_state=OpenShiftState.INSTALLED_TO_INVENTORY,
        mce_id=mce_id,
        agent_id=str(agent_id) if agent_id else None,
    )
