"""`app.infrastructure.providers.ucs_central.provider`.

Exercised against a fake client rather than a live UCS Central, the same
way the UCS Manager provider is. What matters here is the multi-domain
orchestration the UCS Manager collector never had to do: that one query
set covers every registered domain, that descendants are joined back onto
the right server *across* domains, and that the diagnostics which make
Central's one unverified assumption visible actually fire.

The fake client's DN shapes are the real ones, confirmed from
`ucscsdk==0.9.0.10` MO metadata and stated outright in Cisco's own
`docs/ucscsdk_ug.rst`: inventory is rooted per domain at
`compute/sys-<domainId>`, so a blade is
`compute/sys-1009/chassis-1/blade-1` and its CIMC interface is
`compute/sys-1009/chassis-1/blade-1/mgmt/if-1`. A regression to the UCS
Manager `sys/...` root, or to a per-server child query, would fail here.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from structlog.testing import capture_logs

from app.domain.enums import ManagerType
from app.domain.models.common import AuditFields
from app.domain.models.manager import Manager
from app.domain.ports.credentials import ManagerConnection
from app.infrastructure.providers.ucs_central.provider import (
    UcsCentralProvider,
    domain_id_from_dn,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("dn", "expected"),
    [
        ("compute/sys-1009/chassis-1/blade-1", "1009"),
        ("compute/sys-1009/rack-unit-3", "1009"),
        ("compute/sys-1009", "1009"),
        # Global objects live outside any domain's subtree — an org's
        # service profiles, for instance — and must not be attributed to
        # one.
        ("org-root/ls-ocp4-prod-one-infra-01", None),
        ("extpol/reg/clients/client-1009", None),
        ("compute", None),
        ("", None),
    ],
)
def test_domain_id_from_dn(dn: str, expected: str | None) -> None:
    assert domain_id_from_dn(dn) == expected


def _blade(domain: str, chassis: int, slot: int, **props: Any) -> SimpleNamespace:
    dn = f"compute/sys-{domain}/chassis-{chassis}/blade-{slot}"
    defaults: dict[str, Any] = {
        "dn": dn,
        "presence": "equipped",
        "name": "",  # empty in practice — the name comes from the profile
        "serial": f"SER{domain}{slot}",
        "model": "UCSB-B200-M5",
        "num_of_cpus": "2",
        "num_of_cores": "24",
        "num_of_threads": "48",
        "total_memory": "262144",
        "uuid": f"uuid-{domain}-{slot}",
        "assigned_to_dn": "",
    }
    defaults.update(props)
    return SimpleNamespace(**defaults)


def _profile(name: str, pn_dn: str) -> SimpleNamespace:
    return SimpleNamespace(
        dn=f"org-root/ls-{name}",
        name=name,
        type="instance",
        pn_dn=pn_dn,
        src_templ_name="",
        oper_src_templ_name="",
    )


def _domain(domain_id: str, name: str, **props: Any) -> SimpleNamespace:
    defaults: dict[str, Any] = {
        "dn": f"compute/sys-{domain_id}",
        "id": domain_id,
        "name": name,
        "address": f"10.0.0.{domain_id[-1]}",
        "inventory_status": "fine",
        "last_refreshed_ts": "2026-08-15T00:00:00",
        "total_physical_cnt": "1",
    }
    defaults.update(props)
    return SimpleNamespace(**defaults)


class FakeClient:
    """Stands in for `UcsCentralClient`, recording the call sequence so
    the session lifecycle can be asserted.
    """

    def __init__(self, responses: dict[str, list[Any]]) -> None:
        self._responses = responses
        self.calls: list[str] = []

    async def login(self) -> None:
        self.calls.append("login")

    async def logout(self) -> None:
        self.calls.append("logout")

    async def query_classid(self, class_id: str) -> list[Any]:
        self.calls.append(f"query:{class_id}")
        return list(self._responses.get(class_id, []))


async def _collect_with_logs(
    provider: UcsCentralProvider,
) -> tuple[list[Any], list[dict[str, Any]]]:
    """Collect, capturing structlog events as dicts.

    `structlog.testing.capture_logs` rather than `capsys`/`caplog`:
    whether these events render to stdout or through stdlib `logging`
    depends on which logging configuration is active, which differs
    between running this file alone and running the whole suite. Asserting
    on the event payload instead of on rendered text makes the test
    independent of that entirely.
    """
    with capture_logs() as events:
        servers = [ps async for ps in provider.list_servers()]
    return servers, events


def _events(events: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    return [e for e in events if e.get("event") == name]


def _provider(client: FakeClient) -> UcsCentralProvider:
    provider = UcsCentralProvider(
        manager=Manager(
            _id="mgr_ucs_central",
            name="ucs-central",
            type=ManagerType.UCS_CENTRAL,
            endpoint="central.example.com",
            audit=AuditFields.new(),
        ),
        credentials=ManagerConnection(
            endpoint="central.example.com", username="admin", password="secret"
        ),
        timeout_seconds=5.0,
    )
    provider._new_client = lambda: client  # type: ignore[method-assign]
    return provider


async def _collect(provider: UcsCentralProvider) -> list[Any]:
    return [ps async for ps in provider.list_servers()]


class TestMultiDomain:
    async def test_collects_servers_from_every_registered_domain(self) -> None:
        """The whole reason this provider exists: one login, every domain.
        A UCS Manager connection reaches exactly one.
        """
        client = FakeClient(
            {
                "computeSystem": [_domain("1009", "dc1-a"), _domain("1010", "dc1-b")],
                "computeBlade": [
                    _blade("1009", 1, 1, assigned_to_dn="org-root/ls-ocp4-prod-one-infra-01"),
                    _blade("1010", 1, 1, assigned_to_dn="org-root/ls-ocp4-prod-two-infra-01"),
                ],
                "lsServer": [
                    _profile("ocp4-prod-one-infra-01", "compute/sys-1009/chassis-1/blade-1"),
                    _profile("ocp4-prod-two-infra-01", "compute/sys-1010/chassis-1/blade-1"),
                ],
            }
        )
        servers = await _collect(_provider(client))

        assert [s.name for s in servers] == [
            "ocp4-prod-one-infra-01",
            "ocp4-prod-two-infra-01",
        ]
        # external_id keeps the domain-qualified DN, so two blades in the
        # same chassis slot in different domains never collide.
        assert [s.external_id for s in servers] == [
            "compute/sys-1009/chassis-1/blade-1",
            "compute/sys-1010/chassis-1/blade-1",
        ]

    async def test_descendants_join_to_their_own_domains_server(self) -> None:
        """Identical chassis/slot numbering across domains is the norm, so
        a DN-prefix join that ignored the domain segment would attach
        domain 1010's NIC to domain 1009's blade.
        """
        client = FakeClient(
            {
                "computeSystem": [_domain("1009", "a"), _domain("1010", "b")],
                "computeBlade": [_blade("1009", 1, 1), _blade("1010", 1, 1)],
                "mgmtIf": [
                    SimpleNamespace(
                        dn="compute/sys-1009/chassis-1/blade-1/mgmt/if-1",
                        access="unspecified",
                        ext_ip="10.1.1.1",
                        mac="00:11:22:33:44:01",
                    ),
                    SimpleNamespace(
                        dn="compute/sys-1010/chassis-1/blade-1/mgmt/if-1",
                        access="unspecified",
                        ext_ip="10.1.1.2",
                        mac="00:11:22:33:44:02",
                    ),
                ],
                "adaptorExtEthIf": [
                    SimpleNamespace(
                        dn="compute/sys-1010/chassis-1/blade-1/adaptor-1/ext-eth-1",
                        mac="AA:BB:CC:DD:EE:10",
                        switch_id="A",
                        admin_state="enabled",
                        oper_state="up",
                        name="ext-eth-1",
                        id="1",
                        peer_dn="",
                    )
                ],
            }
        )
        servers = await _collect(_provider(client))

        by_dn = {s.external_id: s for s in servers}
        first = by_dn["compute/sys-1009/chassis-1/blade-1"]
        second = by_dn["compute/sys-1010/chassis-1/blade-1"]
        assert first.bmc_address_raw == "ipmi://10.1.1.1:623"
        assert second.bmc_address_raw == "ipmi://10.1.1.2:623"
        # Only domain 1010's blade has an adapter interface.
        assert first.nic_macs == ()
        assert second.nic_macs == ("AA:BB:CC:DD:EE:10",)

    async def test_queries_each_class_once_for_the_whole_fleet(self) -> None:
        """Cost is per-class, not per-domain or per-server — the property
        that makes Central cheaper than N UCS Manager logins.
        """
        client = FakeClient(
            {
                "computeSystem": [_domain(str(1000 + i), f"d{i}") for i in range(20)],
                "computeBlade": [_blade(str(1000 + i), 1, 1) for i in range(20)],
            }
        )
        await _collect(_provider(client))

        queries = [c for c in client.calls if c.startswith("query:")]
        assert len(queries) == len(set(queries)) == 7
        assert client.calls[0] == "login"
        assert client.calls[-1] == "logout"

    async def test_attachments_are_attributed_to_ucs_central(self) -> None:
        client = FakeClient(
            {
                "computeSystem": [_domain("1009", "a")],
                "computeBlade": [_blade("1009", 1, 1)],
                "adaptorExtEthIf": [
                    SimpleNamespace(
                        dn="compute/sys-1009/chassis-1/blade-1/adaptor-1/ext-eth-1",
                        mac="AA:BB:CC:DD:EE:01",
                        switch_id="B",
                        admin_state="enabled",
                        oper_state="up",
                        name="ext-eth-1",
                        id="1",
                        peer_dn="compute/sys-1009/switch-B/slot-1/port-1",
                    )
                ],
            }
        )
        (server,) = await _collect(_provider(client))
        assert server.attachments[0].provider == ManagerType.UCS_CENTRAL.value
        assert server.attachments[0].fabric == "B"


class TestSessionLifecycle:
    async def test_logout_runs_even_when_a_query_fails(self) -> None:
        """Central enforces a per-user session cap, so a leaked session
        costs the *next* run, not this one — which makes it easy to miss.
        """

        class Failing(FakeClient):
            async def query_classid(self, class_id: str) -> list[Any]:
                self.calls.append(f"query:{class_id}")
                raise RuntimeError("boom")

        client = Failing({})
        with pytest.raises(RuntimeError, match="boom"):
            await _collect(_provider(client))
        assert client.calls[-1] == "logout"

    async def test_logout_runs_even_when_login_fails(self) -> None:
        class FailingLogin(FakeClient):
            async def login(self) -> None:
                self.calls.append("login")
                raise RuntimeError("bad credentials")

        client = FailingLogin({})
        with pytest.raises(RuntimeError, match="bad credentials"):
            await _collect(_provider(client))
        assert client.calls == ["login", "logout"]


class TestProfileCoverageDiagnostics:
    """The one thing that could not be verified without a live UCS
    Central is whether its `lsServer` includes domain-*local* service
    profiles. If it does not, servers lose their names, fail
    `INVENTORY_COLLECTOR_NAME_PATTERN`, and the inventory comes back
    mysteriously empty. These make that failure say its own name.
    """

    async def test_warns_when_a_domain_resolves_no_service_profiles(self) -> None:
        client = FakeClient(
            {
                "computeSystem": [_domain("1009", "has-profiles"), _domain("1010", "no-profiles")],
                "computeBlade": [
                    _blade("1009", 1, 1, assigned_to_dn="org-root/ls-ocp4-prod-one-infra-01"),
                    _blade("1010", 1, 1, assigned_to_dn="org-local/ls-not-in-central"),
                ],
                "lsServer": [
                    _profile("ocp4-prod-one-infra-01", "compute/sys-1009/chassis-1/blade-1")
                ],
            }
        )
        servers, events = await _collect_with_logs(_provider(client))

        # Exactly the domain whose profiles Central did not have — the one
        # that resolved its profile must not be flagged, or the warning is
        # noise on every healthy run and gets ignored.
        (warning,) = _events(events, "ucs_central.domain_without_profiles")
        assert warning["domain_id"] == "1010"
        assert warning["collected_servers"] == 1
        # The unnamed server still falls back to its DN rather than being
        # dropped here — the name filter is the collector's job.
        assert servers[1].name == "compute/sys-1010/chassis-1/blade-1"

    async def test_warns_about_servers_in_a_domain_central_did_not_list(self) -> None:
        """Inventory present for a domain absent from `computeSystem`
        would otherwise never appear in the per-domain summary at all.
        """
        client = FakeClient(
            {
                "computeSystem": [_domain("1009", "listed")],
                "computeBlade": [_blade("1009", 1, 1), _blade("9999", 1, 1)],
            }
        )
        _, events = await _collect_with_logs(_provider(client))

        (warning,) = _events(events, "ucs_central.servers_in_unlisted_domain")
        assert warning["domain_ids"] == ["9999"]
        assert warning["servers"] == 1

    async def test_every_domain_gets_a_summary_line(self) -> None:
        """Including a domain Central lists but collected nothing from —
        `reported_servers` vs `collected_servers` is the only signal that
        a domain's replica never arrived.
        """
        client = FakeClient(
            {
                "computeSystem": [
                    _domain("1009", "healthy"),
                    _domain("1010", "never-synced", inventory_status="in-progress"),
                ],
                "computeBlade": [_blade("1009", 1, 1)],
            }
        )
        _, events = await _collect_with_logs(_provider(client))

        summaries = _events(events, "ucs_central.domain_summary")
        assert len(summaries) == 2
        stalled = next(e for e in summaries if e["domain_id"] == "1010")
        assert stalled["collected_servers"] == 0
        assert stalled["reported_servers"] == "1"
        assert stalled["inventory_status"] == "in-progress"

    async def test_non_primary_blades_are_not_double_counted(self) -> None:
        """A multi-node server's slave half is physically present but not
        independently addressable — shared with the UCS Manager collector
        via `ucs_common.is_equipped`.
        """
        client = FakeClient(
            {
                "computeSystem": [_domain("1009", "a")],
                "computeBlade": [
                    _blade("1009", 1, 1),
                    _blade("1009", 1, 2, presence="equipped-slave"),
                    _blade("1009", 1, 3, presence="missing"),
                ],
            }
        )
        servers = await _collect(_provider(client))
        assert [s.external_id for s in servers] == ["compute/sys-1009/chassis-1/blade-1"]
