"""Resolves a manager type's connection details from application settings.

Settings are environment-sourced (`app.config.settings.Settings`, prefix
`INVENTORY_`), so in Kubernetes these arrive from a `Secret` via
`envFrom` — the values live in the Helm chart's `values.yaml`, get
rendered into a Secret, and never appear in the pod spec itself.

The mappings below are explicit rather than derived from the enum member
name (`ManagerType.UCS_MANAGER` -> `ucs_manager_*`). A convention would
work today and silently break the day a member is renamed: the rename
would change which environment variables a deployment must set, with
nothing failing until a collector runs against a live domain and reports
"not configured". Naming the fields makes that a type error instead.

**A login and an endpoint are separate questions**, which is why there
are two maps rather than one three-tuple. UCS Manager has a login but no
configured endpoint: it is never pointed at directly any more, it is
reached once per registered domain by the UCS Central collector, which
gets each domain's address from Central itself
(`ComputeSystem.address`). A single `_SETTINGS_FIELDS` tuple could only
express that with an empty-string or `None` endpoint field, which would
make `resolve()` fail with "set INVENTORY_UCS_MANAGER_IP" — naming a
variable that does not exist. Splitting the maps makes the absence a
fact of the type instead of a runtime surprise.
"""

from __future__ import annotations

from app.config.settings import Settings
from app.domain.enums import ManagerType
from app.domain.ports.credentials import (
    ManagerConnection,
    ManagerNotConfiguredError,
)

# manager type -> the two `Settings` fields holding its credential. Every
# type this platform knows about is here, so the values file is uniform
# across vendors.
#
# For most vendors that pair is a username and a password. **Intersight's
# is not**: it signs each request with an API key, so its two fields are
# an API Key ID and a PEM private key, and they are named that way. The
# *shape* stays a pair so one Secret and one values block serve every
# vendor; only the field names differ, which is what makes
# `ManagerNotConfiguredError` name the right variable to set.
_LOGIN_FIELDS: dict[ManagerType, tuple[str, str]] = {
    ManagerType.UCS_MANAGER: ("ucs_manager_username", "ucs_manager_password"),
    ManagerType.UCS_CENTRAL: ("ucs_central_username", "ucs_central_password"),
    ManagerType.ONEVIEW: ("oneview_username", "oneview_password"),
    ManagerType.OPENMANAGE: ("ome_username", "ome_password"),
    ManagerType.INTERSIGHT: ("intersight_api_key_id", "intersight_api_key_pem"),
    ManagerType.REDFISH_STANDALONE: ("redfish_username", "redfish_password"),
}

# manager type -> the `Settings` field holding the address to connect to.
#
# `UCS_MANAGER` is deliberately absent, and that absence is the whole
# point of this map being separate: a UCS Manager domain is reached by the
# UCS Central collector, once per domain, at the address Central reports
# for it. There is no `INVENTORY_UCS_MANAGER_IP` to set, so `resolve()`
# says that in as many words rather than demanding a variable that no
# longer exists.
# `REDFISH_STANDALONE` is absent for the same reason `UCS_MANAGER` is,
# and it is the second instance of the shape this split exists to
# express: it has a fleet-wide fallback login but no single endpoint,
# because its endpoints are the hosts in its inventory file. See
# docs/adr/0016-redfish-standalone-collector.md.
_ENDPOINT_FIELD: dict[ManagerType, str] = {
    ManagerType.UCS_CENTRAL: "ucs_central_ip",
    ManagerType.ONEVIEW: "oneview_ip",
    ManagerType.OPENMANAGE: "ome_ip",
    ManagerType.INTERSIGHT: "intersight_ip",
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
        if manager_type is ManagerType.UCS_MANAGER:
            raise ManagerNotConfiguredError(
                "UCS_MANAGER has no endpoint of its own to configure — each domain is "
                "reached through UCS Central, which reports its address. Configure "
                "INVENTORY_UCS_CENTRAL_IP/_USERNAME/_PASSWORD plus the fleet-wide "
                "INVENTORY_UCS_MANAGER_USERNAME/_PASSWORD, and collect with "
                "--manager-type UCS_CENTRAL."
            )
        if manager_type is ManagerType.REDFISH_STANDALONE:
            raise ManagerNotConfiguredError(
                "REDFISH_STANDALONE has no endpoint of its own to configure — it reaches one "
                "BMC at a time from an inventory file. Set INVENTORY_REDFISH_INVENTORY_FILE to "
                "that file, give each host a credential in "
                "INVENTORY_REDFISH_CREDENTIALS_FILE, and/or set INVENTORY_REDFISH_USERNAME and "
                "INVENTORY_REDFISH_PASSWORD as the fleet-wide fallback login."
            )
        endpoint_field = _ENDPOINT_FIELD.get(manager_type)
        login_fields = _LOGIN_FIELDS.get(manager_type)
        if endpoint_field is None or login_fields is None:
            raise ManagerNotConfiguredError(
                f"{manager_type.value} has no connection configuration defined."
            )

        username_field, password_field = login_fields
        fields = (endpoint_field, username_field, password_field)
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


def resolve_login(settings: Settings, manager_type: ManagerType) -> tuple[str, str]:
    """A manager type's `(username, password)` **without** its endpoint.

    For the one collector that discovers its endpoints at runtime instead
    of reading one from configuration: the UCS Central collector logs into
    each registered domain's UCS Manager, and Central itself supplies those
    addresses (`ComputeSystem.address`). One UCS Manager service account is
    valid across the domains of one fleet, so this is a single login used
    many times, not a login per domain.

    Same error shape as `EnvConnectionResolver.resolve`: a missing value
    names the environment variable to set.
    """
    login_fields = _LOGIN_FIELDS.get(manager_type)
    if login_fields is None:
        raise ManagerNotConfiguredError(
            f"{manager_type.value} has no connection configuration defined."
        )
    username_field, password_field = login_fields
    username = str(getattr(settings, username_field, "") or "").strip()
    password = str(getattr(settings, password_field, "") or "").strip()

    missing = [
        _env_var(field)
        for field, value in ((username_field, username), (password_field, password))
        if not value
    ]
    if missing:
        raise ManagerNotConfiguredError(
            f"{manager_type.value} has no login configured — set {', '.join(sorted(missing))}."
        )
    return username, password


def configured_manager_types(settings: Settings) -> list[ManagerType]:
    """Every manager type this deployment can be pointed at directly.

    Used to report what a deployment actually has configured, without each
    caller re-implementing "is this one filled in".

    `UCS_MANAGER` can never appear, by construction rather than by
    accident: it is absent from `_ENDPOINT_FIELD`, so it has no address to
    be pointed at and is collected only as part of a `UCS_CENTRAL` run.
    Iterating the endpoint map rather than the login map is what makes
    that a statement of intent instead of a `resolve()` call that is
    guaranteed to fail.
    """
    resolver = EnvConnectionResolver(settings)
    configured: list[ManagerType] = []
    for manager_type in _ENDPOINT_FIELD:
        try:
            resolver.resolve(manager_type)
        except ManagerNotConfiguredError:
            continue
        configured.append(manager_type)
    return configured
