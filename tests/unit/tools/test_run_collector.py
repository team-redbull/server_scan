"""`tools.run_collector` — the collector CLI's own logic.

Everything covered here is pure decision-making (which provider a manager
maps to, what happens when one is missing or misconfigured, whether one
bad manager takes down the rest of the run) and needs neither MongoDB nor
a vendor endpoint.
"""

from __future__ import annotations

from typing import Any

import pytest
from tools.run_collector import (
    _build_provider,
    _dry_run_one_manager,
    _filtered,
    _parse_args,
    _run_one_manager,
)

from app.domain.enums import ManagerType
from app.domain.models.common import AuditFields
from app.domain.models.manager import Manager
from app.domain.ports.credentials import ManagerConnection, ManagerNotConfiguredError
from app.domain.ports.provider import ProviderServer

pytestmark = pytest.mark.unit


def _manager(**overrides: Any) -> Manager:
    defaults: dict[str, Any] = {
        "_id": "mgr-1",
        "name": "ucsm-lab",
        "type": ManagerType.UCS_MANAGER,
        "site_id": "site-1",
        "endpoint": "ucsm.lab.example.com",
        "audit": AuditFields.new(),
    }
    defaults.update(overrides)
    return Manager(**defaults)


class FakeCredentialResolver:
    def __init__(self, *, error: Exception | None = None) -> None:
        self._error = error
        self.resolved: list[ManagerType] = []

    def resolve(self, manager_type: ManagerType) -> ManagerConnection:
        self.resolved.append(manager_type)
        if self._error is not None:
            raise self._error
        return ManagerConnection(
            endpoint="ucsm.lab.example.com", username="admin", password="secret"
        )


class TestBuildProvider:
    async def test_builds_a_provider_for_ucs_manager(self) -> None:
        resolver = FakeCredentialResolver()
        provider = await _build_provider(
            _manager(), credential_resolver=resolver, timeout_seconds=5.0
        )
        assert provider.provider_type == ManagerType.UCS_MANAGER.value
        assert resolver.resolved == [ManagerType.UCS_MANAGER]

    @pytest.mark.parametrize(
        "manager_type",
        [ManagerType.OPENMANAGE, ManagerType.INTERSIGHT, ManagerType.ONEVIEW],
    )
    async def test_unimplemented_vendors_fail_loudly(self, manager_type: ManagerType) -> None:
        """A missing collector must be an explicit error, never a silent
        no-op that looks like a manager with zero servers.
        """
        with pytest.raises(NotImplementedError, match="No collector implemented"):
            await _build_provider(
                _manager(type=manager_type),
                credential_resolver=FakeCredentialResolver(),
                timeout_seconds=5.0,
            )

    async def test_builds_a_provider_for_ucs_central(self) -> None:
        """UCS Central is a collection source in its own right, not only a
        discovery parent: one login covers every registered domain, which
        is the only way this tool reaches a multi-domain fleet given it
        resolves exactly one endpoint per manager type.
        """
        resolver = FakeCredentialResolver()
        provider = await _build_provider(
            _manager(type=ManagerType.UCS_CENTRAL),
            credential_resolver=resolver,
            timeout_seconds=5.0,
        )
        assert provider.provider_type == ManagerType.UCS_CENTRAL.value
        assert resolver.resolved == [ManagerType.UCS_CENTRAL]

    async def test_unconfigured_manager_type_is_rejected_before_connecting(self) -> None:
        resolver = FakeCredentialResolver(error=ManagerNotConfiguredError("not configured"))
        with pytest.raises(ManagerNotConfiguredError):
            await _build_provider(_manager(), credential_resolver=resolver, timeout_seconds=5.0)

    async def test_missing_endpoint_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="no endpoint"):
            await _build_provider(
                _manager(endpoint=None),
                credential_resolver=FakeCredentialResolver(),
                timeout_seconds=5.0,
            )


class FakeIngestService:
    def __init__(self, *, error: Exception | None = None) -> None:
        self._error = error
        self.ingested = 0

    async def ingest(self, provider: Any) -> Any:
        self.ingested += 1
        if self._error is not None:
            raise self._error
        return "summary"


class TestRunOneManager:
    async def test_returns_the_ingest_summary(self) -> None:
        ingest = FakeIngestService()
        result = await _run_one_manager(
            _manager(),
            ingest_service=ingest,  # type: ignore[arg-type]
            credential_resolver=FakeCredentialResolver(),  # type: ignore[arg-type]
            timeout_seconds=5.0,
        )
        assert result == "summary"

    async def test_does_not_health_check_separately_from_ingest(self) -> None:
        """`IngestService.ingest()` health-checks as its first step, and a
        UCS login is ~4 round trips — collecting the health check here too
        would double that and burn a second session per manager.
        """
        calls: list[str] = []

        class RecordingIngest(FakeIngestService):
            async def ingest(self, provider: Any) -> Any:
                calls.append("ingest")
                return "summary"

        provider_health_checks: list[str] = []

        async def _fail_if_called() -> None:
            provider_health_checks.append("health_check")

        ingest = RecordingIngest()
        manager = _manager()
        resolver = FakeCredentialResolver()
        provider = await _build_provider(manager, credential_resolver=resolver, timeout_seconds=5.0)
        provider.health_check = _fail_if_called  # type: ignore[method-assign]

        await _run_one_manager(
            manager,
            ingest_service=ingest,  # type: ignore[arg-type]
            credential_resolver=resolver,  # type: ignore[arg-type]
            timeout_seconds=5.0,
        )
        assert calls == ["ingest"]
        assert provider_health_checks == []

    @pytest.mark.parametrize(
        "error",
        [RuntimeError("unreachable"), ManagerNotConfiguredError("not set"), ValueError("bad")],
    )
    async def test_a_failing_manager_is_isolated_not_propagated(self, error: Exception) -> None:
        """One flaky domain must not abort the run for the other managers
        of the same type — the caller distinguishes failure by `None`.
        """
        result = await _run_one_manager(
            _manager(),
            ingest_service=FakeIngestService(error=error),  # type: ignore[arg-type]
            credential_resolver=FakeCredentialResolver(),  # type: ignore[arg-type]
            timeout_seconds=5.0,
        )
        assert result is None

    async def test_an_unimplemented_vendor_is_reported_as_a_failure(self) -> None:
        result = await _run_one_manager(
            _manager(type=ManagerType.ONEVIEW),
            ingest_service=FakeIngestService(),  # type: ignore[arg-type]
            credential_resolver=FakeCredentialResolver(),  # type: ignore[arg-type]
            timeout_seconds=5.0,
        )
        assert result is None


def _factory(provider: Any) -> Any:
    """A stand-in for `_build_provider` that hands back a ready-made
    provider, so the dry-run path can be tested without a UCS domain."""

    async def build(_manager: Any, **_kwargs: Any) -> Any:
        return provider

    return build


class TestDryRun:
    async def test_dry_run_reports_servers_without_ingesting(self, capsys: Any) -> None:
        """The whole point of --dry-run is that it never reaches the
        pipeline: no classification, no health evaluation, no audit
        events, no upsert.
        """

        class FakeProvider:
            provider_type = "UCS_MANAGER"

            async def health_check(self) -> None:
                return None

            async def list_servers(self) -> Any:
                for name in ("ocp4-prod-one-infra-01", "ocp4-hypershift-five-01"):
                    yield ProviderServer(external_id=f"dn/{name}", vendor="cisco", name=name)

        count = await _dry_run_one_manager(
            _manager(),
            credential_resolver=FakeCredentialResolver(),  # type: ignore[arg-type]
            timeout_seconds=5.0,
            limit=None,
            provider_factory=_factory(FakeProvider()),
        )
        assert count == 2
        out = capsys.readouterr().out
        # The site each name resolves to is shown, since that is derived
        # at ingest and is otherwise invisible until after a real write.
        assert "ocp4-prod-one-infra-01" in out
        assert "one" in out
        assert "five" in out
        assert "Nothing was written" in out

    async def test_dry_run_respects_limit(self, capsys: Any) -> None:
        class FakeProvider:
            provider_type = "UCS_MANAGER"

            async def health_check(self) -> None:
                return None

            async def list_servers(self) -> Any:
                for i in range(10):
                    yield ProviderServer(external_id=f"dn/{i}", vendor="cisco", name=f"srv-{i}")

        count = await _dry_run_one_manager(
            _manager(),
            credential_resolver=FakeCredentialResolver(),  # type: ignore[arg-type]
            timeout_seconds=5.0,
            limit=3,
            provider_factory=_factory(FakeProvider()),
        )
        assert count == 3
        assert "stopped at --limit 3" in capsys.readouterr().out


class TestNameFilter:
    """`INVENTORY_COLLECTOR_NAME_PATTERN` — a vendor manager holds the
    whole datacenter, so this is what decides which of its servers are
    this platform's at all.
    """

    class _Fake:
        provider_type = "UCS_MANAGER"

        def __init__(self, *names: str) -> None:
            self._names = names
            self.health_checked = 0

        async def health_check(self) -> None:
            self.health_checked += 1

        async def list_servers(self) -> Any:
            for name in self._names:
                yield ProviderServer(external_id=f"dn/{name}", vendor="cisco", name=name)

    async def _names_through(self, pattern: str, *names: str) -> list[str]:
        provider = _filtered(self._Fake(*names), pattern)  # type: ignore[arg-type]
        return [ps.name async for ps in provider.list_servers()]

    async def test_keeps_only_matching_servers(self) -> None:
        kept = await self._names_through(
            "^ocp",
            "ocp4-prod-one-infra-01",
            "vmhost-two-14",
            "ocp4-hypershift-five-01",
            "db-prod-03",
        )
        assert kept == ["ocp4-prod-one-infra-01", "ocp4-hypershift-five-01"]

    async def test_the_anchor_is_the_operators_to_write(self) -> None:
        """`re.search`, not `re.match` — so `^ocp` means "starts with" and
        an unanchored pattern stays a substring match, rather than the
        code silently anchoring something the operator didn't ask for.
        A name merely *containing* "ocp" is not an OCP server.
        """
        assert await self._names_through("^ocp", "legacy-ocp-gateway-01") == []
        assert await self._names_through("ocp", "legacy-ocp-gateway-01") == [
            "legacy-ocp-gateway-01"
        ]

    async def test_an_empty_pattern_collects_everything(self) -> None:
        """Not "matches nothing" — an empty regex matches every string,
        but `_filtered` doesn't even wrap, so the default is unambiguously
        "no filter" rather than an accidental empty inventory.
        """
        fake = self._Fake("srv-1", "srv-2")
        assert _filtered(fake, "") is fake  # type: ignore[arg-type]

    async def test_health_check_still_reaches_the_real_provider(self) -> None:
        """The wrapper stands in for the provider everywhere, including
        the login `IngestService.ingest()` performs first.
        """
        fake = self._Fake()
        await _filtered(fake, "^ocp").health_check()  # type: ignore[arg-type]
        assert fake.health_checked == 1

    async def test_dry_run_shows_only_what_a_real_run_would_write(self, capsys: Any) -> None:
        """--dry-run bypasses `IngestService` on purpose, so the filter
        has to live on the provider side or a dry run would print servers
        a real run silently drops.
        """
        count = await _dry_run_one_manager(
            _manager(),
            credential_resolver=FakeCredentialResolver(),  # type: ignore[arg-type]
            timeout_seconds=5.0,
            limit=None,
            name_pattern="^ocp",
            provider_factory=_factory(self._Fake("ocp4-prod-one-infra-01", "vmhost-two-14")),
        )
        assert count == 1
        out = capsys.readouterr().out
        assert "ocp4-prod-one-infra-01" in out
        assert "vmhost-two-14" not in out
        assert "^ocp" in out


class TestParseArgs:
    def test_manager_type_is_required(self) -> None:
        with pytest.raises(SystemExit):
            _parse_args([])

    def test_rejects_an_unknown_manager_type(self) -> None:
        with pytest.raises(SystemExit):
            _parse_args(["--manager-type", "NOT_A_VENDOR"])

    def test_accepts_a_known_manager_type(self) -> None:
        assert _parse_args(["--manager-type", "UCS_MANAGER"]).manager_type == "UCS_MANAGER"

    def test_dry_run_and_debug_flags_default_off(self) -> None:
        args = _parse_args(["--manager-type", "UCS_MANAGER"])
        assert args.dry_run is False
        assert args.debug_xml is False
        assert args.limit is None
