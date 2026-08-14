"""Resolves a manager type's connection details from application settings.

Settings are environment-sourced (`app.config.settings.Settings`, prefix
`INVENTORY_`), so in Kubernetes these arrive from a `Secret` via
`envFrom` — the values live in the Helm chart's `values.yaml`, get
rendered into a Secret, and never appear in the pod spec itself.

The mapping below is explicit rather than derived from the enum member
name (`ManagerType.UCS_MANAGER` -> `ucs_manager_*`). A convention would
work today and silently break the day a member is renamed: the rename
would change which environment variables a deployment must set, with
nothing failing until a collector runs against a live domain and reports
"not configured". Naming the fields makes that a type error instead.
"""

from __future__ import annotations

from app.config.settings import Settings
from app.domain.enums import ManagerType
from app.domain.ports.credentials import (
    ManagerConnection,
    ManagerNotConfiguredError,
)

# manager type -> the `Settings` field names holding its endpoint, user
# and password. Every type this platform knows about is here, so the
# values file is uniform across vendors; a type absent from the map has
# no configuration at all and `resolve` says so explicitly.
#
# Intersight reuses the same three fields with different meanings — API
# Key ID and secret key rather than a login. That is a documentation
# problem, not a shape problem: the collector for it knows how to sign
# with them, and keeping one shape means one Secret and one values block
# per vendor rather than a special case.
_SETTINGS_FIELDS: dict[ManagerType, tuple[str, str, str]] = {
    ManagerType.UCS_MANAGER: ("ucs_manager_ip", "ucs_manager_username", "ucs_manager_password"),
    ManagerType.UCS_CENTRAL: ("ucs_central_ip", "ucs_central_username", "ucs_central_password"),
    ManagerType.ONEVIEW: ("oneview_ip", "oneview_username", "oneview_password"),
    ManagerType.OPENMANAGE: ("ome_ip", "ome_username", "ome_password"),
    ManagerType.INTERSIGHT: ("intersight_ip", "intersight_username", "intersight_password"),
}


def _env_var(field: str) -> str:
    """The environment variable a settings field reads, for error
    messages — the whole point of the message is to name the thing the
    operator has to go and set.
    """
    return f"INVENTORY_{field.upper()}"


class EnvConnectionResolver:
    """Implements `app.domain.ports.credentials.CredentialResolver`."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def resolve(self, manager_type: ManagerType) -> ManagerConnection:
        fields = _SETTINGS_FIELDS.get(manager_type)
        if fields is None:
            raise ManagerNotConfiguredError(
                f"{manager_type.value} has no connection configuration defined."
            )

        endpoint_field, username_field, password_field = fields
        values = {name: str(getattr(self._settings, name, "") or "").strip() for name in fields}

        missing = [_env_var(name) for name, value in values.items() if not value]
        if missing:
            raise ManagerNotConfiguredError(
                f"{manager_type.value} is not configured — set {', '.join(sorted(missing))}."
            )

        return ManagerConnection(
            endpoint=values[endpoint_field],
            username=values[username_field],
            password=values[password_field],
        )


def configured_manager_types(settings: Settings) -> list[ManagerType]:
    """Every manager type with a complete set of connection details.

    Used to report what a deployment actually has configured, without
    each caller re-implementing "is this one filled in".
    """
    resolver = EnvConnectionResolver(settings)
    configured: list[ManagerType] = []
    for manager_type in _SETTINGS_FIELDS:
        try:
            resolver.resolve(manager_type)
        except ManagerNotConfiguredError:
            continue
        configured.append(manager_type)
    return configured
