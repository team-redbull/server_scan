"""Reconciling one cluster's view against the inventory.

The behaviour worth pinning is not "it writes what it saw" — it is what it
does about what it *did not* see. Nothing in Kubernetes reports a removal,
so a server freed from a cluster simply stops appearing, and the only way
to notice is to compare the cluster's list against the servers already
claiming it.

That comparison is also the dangerous part: a job that frees too widely
would mark in-use machines available. So the scope is tested as carefully
as the happy path.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.application.services.openshift_membership import OpenShiftMembershipService
from app.domain.enums import OpenShiftState, Vendor
from app.domain.models.audit_event import Actor, ActorType
from app.domain.models.common import AuditFields
from app.domain.models.openshift import OpenShiftLifecycle
from app.domain.models.server import Identity, Server
from app.domain.ports.repository import Page
from app.infrastructure.openshift.records import ClusterObservation
from app.utils.timeutil import utcnow

pytestmark = pytest.mark.asyncio


def _server(name: str, *, openshift: OpenShiftLifecycle | None = None) -> Server:
    """
    A stored server with only the fields this service reads.

    Args:
        name (str): The server's name; `name_normalized` mirrors it.
        openshift (OpenShiftLifecycle | None): Existing membership.

    Returns:
        Server: The stored document.
    """
    now = utcnow()
    return Server(
        _id=f"srv_{name}",
        name=name,
        name_normalized=name.lower(),
        identity=Identity(vendor=Vendor.DELL),
        openshift=openshift or OpenShiftLifecycle(),
        audit=AuditFields.new(),
        created_at=now,
        updated_at=now,
    )


class FakeRepo:
    """Serves `list_page` from a list, and records every upsert."""

    def __init__(self, servers: list[Server]) -> None:
        """
        Args:
            servers (list[Server]): The stored fleet.
        """
        self.servers = servers
        self.written: list[Server] = []

    async def list_page(self, **kwargs: Any) -> Page:
        """
        Answer either lookup this service makes.

        Args:
            **kwargs (Any): `list_page`'s keyword arguments.

        Returns:
            Page: Matching servers, unpaginated — the fakes here are small
                enough that one page is the whole answer.
        """
        filters: dict[str, Any] = kwargs["filters"]
        matched = [s for s in self.servers if self._matches(s, filters)]
        return Page(items=matched, next_cursor=None, has_more=False, total_count=None)

    @staticmethod
    def _matches(server: Server, filters: dict[str, Any]) -> bool:
        if "name_normalized" in filters:
            return server.name_normalized == filters["name_normalized"]
        if "openshift.cluster_name" in filters:
            return server.openshift.cluster_name == filters["openshift.cluster_name"]
        if "openshift.mce_id" in filters:
            return server.openshift.mce_id == filters["openshift.mce_id"]
        return False

    async def upsert_with_revision_check(self, server: Server, **_: Any) -> Server:
        """
        Record a write.

        Args:
            server (Server): The document written.

        Returns:
            Server: The same document.
        """
        self.written.append(server)
        return server


class FakeAudit:
    """Records audit calls without a database."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    async def record(self, event_type: Any, **kwargs: Any) -> None:
        """
        Args:
            event_type (Any): The event type.
            **kwargs (Any): The event payload.
        """
        self.events.append((event_type, kwargs))


def _service(repo: FakeRepo, audit: FakeAudit) -> OpenShiftMembershipService:
    """
    Build the service against the fakes.

    Args:
        repo (FakeRepo): The stored fleet.
        audit (FakeAudit): The audit sink.

    Returns:
        OpenShiftMembershipService: The service under test.
    """
    return OpenShiftMembershipService(
        server_repo=repo,  # type: ignore[arg-type]
        audit=audit,  # type: ignore[arg-type]
        actor=Actor(type=ActorType.SYSTEM, id="openshift:test"),
    )


def _seen(hostname: str, *, cluster: str = "ocp4-tlv") -> ClusterObservation:
    """
    One node observation.

    Args:
        hostname (str): The reported hostname.
        cluster (str): The reporting cluster.

    Returns:
        ClusterObservation: An INSTALLED observation.
    """
    return ClusterObservation(
        hostname=hostname,
        lifecycle_state=OpenShiftState.INSTALLED,
        cluster_name=cluster,
        node_name=hostname,
        role="worker",
    )


class TestClaiming:
    """What the cluster reported."""

    async def test_a_reported_server_is_marked_installed(self) -> None:
        repo = FakeRepo([_server("ocp4-tlv-worker-01")])
        summary = await _service(repo, FakeAudit()).reconcile(
            [_seen("ocp4-tlv-worker-01")],
            scope={"openshift.cluster_name": "ocp4-tlv"},
            reported_by="ocp4-tlv",
        )

        assert summary.matched == 1
        assert summary.claimed == 1
        assert repo.written[0].openshift.lifecycle_state is OpenShiftState.INSTALLED
        assert repo.written[0].openshift.cluster_name == "ocp4-tlv"

    async def test_an_unmatched_host_is_reported_never_created(self) -> None:
        """The vendor collectors are the only source of what hardware
        exists. A cluster host the inventory has never seen is a gap worth
        surfacing, not a server to invent from a hostname.
        """
        repo = FakeRepo([])
        summary = await _service(repo, FakeAudit()).reconcile(
            [_seen("ocp4-tlv-worker-99")],
            scope={"openshift.cluster_name": "ocp4-tlv"},
            reported_by="ocp4-tlv",
        )

        assert summary.unmatched == ["ocp4-tlv-worker-99"]
        assert repo.written == []

    async def test_an_unchanged_server_is_not_rewritten(self) -> None:
        """These jobs run every 15 minutes over a fleet that rarely
        changes. Writing unconditionally would bump `revision` on every
        server four times an hour and fill the audit trail with
        non-events.
        """
        stored = _server(
            "ocp4-tlv-worker-01",
            openshift=OpenShiftLifecycle(
                lifecycle_state=OpenShiftState.INSTALLED,
                cluster_name="ocp4-tlv",
                node_name="ocp4-tlv-worker-01",
                role="worker",
            ),
        )
        repo = FakeRepo([stored])
        summary = await _service(repo, FakeAudit()).reconcile(
            [_seen("ocp4-tlv-worker-01")],
            scope={"openshift.cluster_name": "ocp4-tlv"},
            reported_by="ocp4-tlv",
        )

        assert summary.claimed == 0
        assert repo.written == []


class TestFreeing:
    """What the cluster stopped reporting — the half nothing else can do."""

    async def test_a_server_this_cluster_no_longer_lists_is_freed(self) -> None:
        """Nothing reports a removal, so this comparison is the only way a
        freed machine ever stops looking in use.
        """
        gone = _server(
            "ocp4-tlv-worker-02",
            openshift=OpenShiftLifecycle(
                lifecycle_state=OpenShiftState.INSTALLED, cluster_name="ocp4-tlv"
            ),
        )
        repo = FakeRepo([_server("ocp4-tlv-worker-01"), gone])
        summary = await _service(repo, FakeAudit()).reconcile(
            [_seen("ocp4-tlv-worker-01")],
            scope={"openshift.cluster_name": "ocp4-tlv"},
            reported_by="ocp4-tlv",
        )

        assert summary.freed == 1
        freed = [s for s in repo.written if s.name == "ocp4-tlv-worker-02"]
        assert freed[0].openshift.lifecycle_state is OpenShiftState.AVAILABLE
        assert freed[0].openshift.cluster_name is None

    async def test_another_cluster_s_servers_are_never_touched(self) -> None:
        """The property that makes per-cluster deployment safe. A job sees
        only its own cluster, so it may only ever release servers already
        naming that cluster — otherwise every job would free every other
        cluster's machines on every run.
        """
        other = _server(
            "ocp4-nyc-worker-01",
            openshift=OpenShiftLifecycle(
                lifecycle_state=OpenShiftState.INSTALLED, cluster_name="ocp4-nyc"
            ),
        )
        repo = FakeRepo([other])
        summary = await _service(repo, FakeAudit()).reconcile(
            [],
            scope={"openshift.cluster_name": "ocp4-tlv"},
            reported_by="ocp4-tlv",
        )

        assert summary.freed == 0
        assert repo.written == []

    async def test_dry_run_writes_nothing_but_still_counts(self) -> None:
        """`--dry-run` has to answer "what would this change" honestly, or
        it cannot be used to check a first run before it happens.
        """
        gone = _server(
            "ocp4-tlv-worker-02",
            openshift=OpenShiftLifecycle(
                lifecycle_state=OpenShiftState.INSTALLED, cluster_name="ocp4-tlv"
            ),
        )
        repo = FakeRepo([gone])
        summary = await _service(repo, FakeAudit()).reconcile(
            [],
            scope={"openshift.cluster_name": "ocp4-tlv"},
            reported_by="ocp4-tlv",
            dry_run=True,
        )

        assert summary.freed == 1
        assert repo.written == []


class TestAudit:
    """Transitions only."""

    async def test_a_state_change_is_recorded(self) -> None:
        repo = FakeRepo([_server("ocp4-tlv-worker-01")])
        audit = FakeAudit()
        await _service(repo, audit).reconcile(
            [_seen("ocp4-tlv-worker-01")],
            scope={"openshift.cluster_name": "ocp4-tlv"},
            reported_by="ocp4-tlv",
        )

        assert len(audit.events) == 1
        _, payload = audit.events[0]
        assert payload["data"]["from"] == "AVAILABLE"
        assert payload["data"]["to"] == "INSTALLED"

    async def test_a_cluster_rename_writes_without_an_event(self) -> None:
        """The server moved cluster but not state. Worth persisting, not
        worth an audit entry — `OPENSHIFT_STATE_CHANGED` means the state
        changed, and firing it for a field that did not would make the
        event useless for alerting.
        """
        stored = _server(
            "ocp4-tlv-worker-01",
            openshift=OpenShiftLifecycle(
                lifecycle_state=OpenShiftState.INSTALLED,
                cluster_name="ocp4-tlv-old",
                node_name="ocp4-tlv-worker-01",
                role="worker",
            ),
        )
        repo = FakeRepo([stored])
        audit = FakeAudit()
        await _service(repo, audit).reconcile(
            [_seen("ocp4-tlv-worker-01")],
            scope={"openshift.cluster_name": "ocp4-tlv"},
            reported_by="ocp4-tlv",
        )

        assert len(repo.written) == 1
        assert audit.events == []
