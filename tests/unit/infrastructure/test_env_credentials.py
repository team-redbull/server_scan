"""`app.infrastructure.credentials.env` — the one place a collector learns
where a vendor manager is and how to log into it.

The behaviour worth pinning down is the failure shape: a half-configured
vendor must be rejected as a configuration error naming the missing
variables, not attempted as a login that then fails as "bad credentials"
and sends an operator looking in the wrong place.
"""

from __future__ import annotations

import pytest

from app.config.settings import Settings
from app.domain.enums import ManagerType
from app.domain.ports.credentials import ManagerNotConfiguredError
from app.infrastructure.credentials.env import (
    EnvConnectionResolver,
    configured_manager_types,
)

pytestmark = pytest.mark.unit


def _settings(**overrides: str) -> Settings:
    # `_env_file=None` so a developer's real .env can't leak into the test.
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


UCSM = {
    "ucs_manager_ip": "192.168.22.128",
    "ucs_manager_username": "ucspe",
    "ucs_manager_password": "s3cret",
}


class TestResolve:
    def test_returns_the_configured_connection(self) -> None:
        connection = EnvConnectionResolver(_settings(**UCSM)).resolve(ManagerType.UCS_MANAGER)
        assert connection.endpoint == "192.168.22.128"
        assert connection.username == "ucspe"
        assert connection.password == "s3cret"

    @pytest.mark.parametrize(
        ("manager_type", "values"),
        [
            (ManagerType.UCS_CENTRAL, {"ucs_central_ip": "10.0.0.1"}),
            (ManagerType.ONEVIEW, {"oneview_ip": "10.0.0.2"}),
            (ManagerType.OPENMANAGE, {"ome_ip": "10.0.0.3"}),
        ],
    )
    def test_every_vendor_has_its_own_variables(
        self, manager_type: ManagerType, values: dict[str, str]
    ) -> None:
        prefix = next(iter(values)).rsplit("_", 1)[0]
        settings = _settings(**values, **{f"{prefix}_username": "u", f"{prefix}_password": "p"})
        assert EnvConnectionResolver(settings).resolve(manager_type).endpoint == next(
            iter(values.values())
        )

    def test_nothing_configured_names_every_missing_variable(self) -> None:
        with pytest.raises(ManagerNotConfiguredError) as exc:
            EnvConnectionResolver(_settings()).resolve(ManagerType.UCS_MANAGER)
        message = str(exc.value)
        assert "INVENTORY_UCS_MANAGER_IP" in message
        assert "INVENTORY_UCS_MANAGER_USERNAME" in message
        assert "INVENTORY_UCS_MANAGER_PASSWORD" in message

    @pytest.mark.parametrize(
        "missing", ["ucs_manager_ip", "ucs_manager_username", "ucs_manager_password"]
    )
    def test_a_partially_configured_vendor_is_a_configuration_error(self, missing: str) -> None:
        """Half-configured must not reach the vendor. A blank password is
        a real login attempt that fails as bad credentials, which sends
        an operator hunting a password problem that does not exist.
        """
        values = {**UCSM, missing: ""}
        with pytest.raises(ManagerNotConfiguredError) as exc:
            EnvConnectionResolver(_settings(**values)).resolve(ManagerType.UCS_MANAGER)
        assert f"INVENTORY_{missing.upper()}" in str(exc.value)

    def test_whitespace_only_counts_as_missing(self) -> None:
        with pytest.raises(ManagerNotConfiguredError):
            EnvConnectionResolver(_settings(**{**UCSM, "ucs_manager_password": "   "})).resolve(
                ManagerType.UCS_MANAGER
            )

    def test_surrounding_whitespace_is_stripped(self) -> None:
        settings = _settings(**{**UCSM, "ucs_manager_ip": "  192.168.22.128  "})
        assert EnvConnectionResolver(settings).resolve(ManagerType.UCS_MANAGER).endpoint == (
            "192.168.22.128"
        )

    def test_intersight_uses_the_same_three_fields(self) -> None:
        """Intersight signs requests with an API key rather than logging
        in, but reuses the same shape so the values file and Secret stay
        uniform: username carries the API Key ID, password the secret key.
        """
        settings = _settings(
            intersight_ip="intersight.com",
            intersight_username="key-id-123",
            intersight_password="secret-key",
        )
        connection = EnvConnectionResolver(settings).resolve(ManagerType.INTERSIGHT)
        assert connection.endpoint == "intersight.com"
        assert connection.username == "key-id-123"

    def test_every_known_manager_type_is_configurable(self) -> None:
        """Guards against a `ManagerType` being added without anywhere to
        put its connection details — which would surface only when
        someone tried to run its collector.
        """
        for manager_type in ManagerType:
            try:
                EnvConnectionResolver(_settings()).resolve(manager_type)
            except ManagerNotConfiguredError as exc:
                assert "INVENTORY_" in str(exc), f"{manager_type} has no settings fields"


class TestConfiguredManagerTypes:
    def test_lists_only_fully_configured_vendors(self) -> None:
        settings = _settings(**UCSM, oneview_ip="10.0.0.2", oneview_username="u")
        assert configured_manager_types(settings) == [ManagerType.UCS_MANAGER]

    def test_empty_when_nothing_is_configured(self) -> None:
        assert configured_manager_types(_settings()) == []


def test_connection_repr_never_shows_the_password() -> None:
    """A `ManagerConnection` ends up in tracebacks and debugger frames;
    its repr must not carry the secret there.
    """
    connection = EnvConnectionResolver(_settings(**UCSM)).resolve(ManagerType.UCS_MANAGER)
    assert "s3cret" not in repr(connection)
    assert "***" in repr(connection)
