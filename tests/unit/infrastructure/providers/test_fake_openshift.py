"""The fake OpenShift membership pass.

It stands in for two CronJobs that do not exist yet, so these pin the
contracts those jobs will have to honour rather than the randomness.
"""

from __future__ import annotations

import pytest

from app.domain.enums import HealthSeverity, InstallationType, OpenShiftState, Vendor
from app.domain.models.classification import Classification
from app.domain.models.health import Health
from app.domain.models.server import Identity, Server
from app.domain.services.normalize import normalize_text
from app.infrastructure.providers.fake.openshift import openshift_for
from app.utils.timeutil import utcnow

pytestmark = pytest.mark.unit


def _server(
    name: str,
    *,
    server_id: str = "srv_openshift_test",
    installation_type: InstallationType = InstallationType.UNCLASSIFIED,
    site_id: str | None = "tlv",
) -> Server:
    """
    Build one stored server to report on.

    Args:
        name (str): Its hostname.
        server_id (str): Its id, which seeds the per-server RNG.
        installation_type (InstallationType): Its regex classification.
        site_id (str | None): Its site.

    Returns:
        Server: A server the pass can read.
    """
    now = utcnow()
    return Server(
        _id=server_id,
        name=name,
        name_normalized=normalize_text(name),
        identity=Identity(
            vendor=Vendor.DELL,
            serial="OSTEST0001",
            serial_normalized="ostest0001",
            nic_macs=["aa:bb:cc:00:00:01"],
        ),
        site_id=site_id,
        classification=Classification(installation_type=installation_type),
        health=Health(overall=HealthSeverity.HEALTHY),
        created_at=now,
        updated_at=now,
    )


def test_it_is_deterministic_per_server() -> None:
    """Re-running the seeder over the same fleet must not reshuffle it,
    and must not depend on iteration order — the state is drawn from the
    server's own id, not from a shared stream.
    """
    server = _server("random-server-0001")

    assert openshift_for(server) == openshift_for(server)


def test_a_hosted_cluster_node_names_its_cluster_and_its_mce() -> None:
    """Both, because a hosted cluster name is only unique within the MCE
    that runs it.
    """
    state = openshift_for(_server("ocp4-hypershift-tlv-05"))

    assert state.lifecycle_state is OpenShiftState.HOSTED_NODE
    assert state.cluster_name is not None
    assert state.mce_id == "mce-tlv"


def test_a_upi_node_names_a_cluster_but_no_mce() -> None:
    """No MCE manages a UPI cluster, so claiming one would be a fiction
    the real job could never produce.
    """
    state = openshift_for(
        _server("ocp4-prod-tlv-compute-01", installation_type=InstallationType.UPI)
    )

    assert state.lifecycle_state is OpenShiftState.UPI_NODE
    assert state.cluster_name == "upi-tlv"
    assert state.mce_id is None


def test_an_available_agent_has_an_mce_but_no_cluster() -> None:
    """`AVAILABLE` means registered and bound to nothing. `cluster_name`
    stays None: there is no cluster, which is a different claim from a
    cluster whose name went unread.
    """
    states = [
        openshift_for(_server(f"random-server-{i:04d}", server_id=f"srv_avail_{i}"))
        for i in range(40)
    ]
    available = [s for s in states if s.lifecycle_state is OpenShiftState.AVAILABLE]

    assert available, "no server came back available across 40 draws"
    for state in available:
        assert state.cluster_name is None
        assert state.mce_id is not None
        assert state.agent_id is not None


def test_an_unreported_server_carries_no_claims_at_all() -> None:
    """A machine racked but not yet handed to OpenShift. Every field stays
    empty rather than defaulting to something plausible.
    """
    states = [
        openshift_for(_server(f"random-server-{i:04d}", server_id=f"srv_unknown_{i}"))
        for i in range(40)
    ]
    unknown = [s for s in states if s.lifecycle_state is OpenShiftState.UNKNOWN]

    assert unknown, "no server came back unreported across 40 draws"
    for state in unknown:
        assert state.cluster_name is None
        assert state.mce_id is None
        assert state.last_reported_at is None


def test_a_reported_server_always_carries_a_timestamp() -> None:
    """Nothing reports a removal, so `last_reported_at` is the only thing
    that can distinguish a live membership from a stale one. A reported
    state without it would be unfalsifiable.
    """
    for name, installation_type in (
        ("ocp4-hypershift-tlv-05", InstallationType.UNCLASSIFIED),
        ("ocp4-prod-tlv-compute-01", InstallationType.UPI),
    ):
        state = openshift_for(_server(name, installation_type=installation_type))
        assert state.last_reported_at is not None


def test_membership_is_not_forced_to_agree_with_the_classification() -> None:
    """The point of keeping the two apart. A server whose name classifies
    UNCLASSIFIED can still be running in a hosted cluster, and that
    disagreement is the signal — a pass that derived one from the other
    could never produce it.
    """
    state = openshift_for(_server("ocp4-hypershift-tlv-05"))

    assert state.lifecycle_state is OpenShiftState.HOSTED_NODE
