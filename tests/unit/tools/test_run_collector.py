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
    _parse_args,
    _run_one_manager,
)

from app.domain.enums import ManagerType
from app.domain.models.common import AuditFields
from app.domain.models.manager import Manager
from app.domain.ports.credentials import CredentialNotFoundError, ManagerCredentials
from app.domain.ports.provider import ProviderServer

pytestmark = pytest.mark.unit


def _manager(**overrides: Any) -> Manager:
    defaults: dict[str, Any] = {
        "_id": "mgr-1",
        "name": "ucsm-lab",
        "type": ManagerType.UCS_MANAGER,
        "site_id": "site-1",
        "endpoint": "ucsm.lab.example.com",
        "credential_ref": "ucsm-lab-creds",
        "audit": AuditFields.new(),
    }
    defaults.update(overrides)
    return Manager(**defaults)


class FakeCredentialResolver:
    def __init__(self, *, error: Exception | None = None) -> None:
        self._error = error
        self.resolved: list[str] = []

    async def resolve(self, credential_ref: str) -> ManagerCredentials:
        self.resolved.append(credential_ref)
        if self._error is not None:
            raise self._error
        return ManagerCredentials(username="admin", password="secret")


class TestBuildProvider:
    async def test_builds_a_provider_for_ucs_manager(self) -> None:
        resolver = FakeCredentialResolver()
        provider = await _build_provider(
            _manager(), credential_resolver=resolver, timeout_seconds=5.0
        )
        assert provider.provider_type == ManagerType.UCS_MANAGER.value
        assert resolver.resolved == ["ucsm-lab-creds"]

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

    async def test_ucs_central_is_not_a_collection_source(self) -> None:
        """UCS Central is a discovery parent over UCS Manager domains, not
        itself an inventory source.
        """
        with pytest.raises(NotImplementedError):
            await _build_provider(
                _manager(type=ManagerType.UCS_CENTRAL, parent_manager_id=None),
                credential_resolver=FakeCredentialResolver(),
                timeout_seconds=5.0,
            )

    async def test_missing_credential_ref_is_rejected_before_connecting(self) -> None:
        resolver = FakeCredentialResolver()
        with pytest.raises(CredentialNotFoundError, match="no credential_ref"):
            await _build_provider(
                _manager(credential_ref=None), credential_resolver=resolver, timeout_seconds=5.0
            )
        assert resolver.resolved == []

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
        [RuntimeError("unreachable"), CredentialNotFoundError("no secret"), ValueError("bad")],
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
