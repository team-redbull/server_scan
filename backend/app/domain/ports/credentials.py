"""The collector connection seam.

A collector needs three things to reach a vendor manager: where it is, and
a username and password to present. All three are configuration, not
inventory data, so they come from one place per manager *type* — see
`app.infrastructure.credentials.env.EnvConnectionResolver`.

Resolution is by `ManagerType`, not by a per-manager reference. That is a
deliberate narrowing: this platform runs one endpoint per vendor type
(one UCS Manager, one OneView appliance, one OME), and keying the lookup
on the type means onboarding a vendor is "fill in three values", with no
document to create first and nothing that can disagree with itself. UCS
Manager's own multi-domain story is the `UCS_CENTRAL` parent, which
enumerates its domains at collection time — see `Manager`'s docstring —
not a list of endpoints someone maintains by hand.

`CredentialResolver` is a `Protocol` rather than a concrete class so a
deployment that wants a real secret store (Vault, External Secrets) can
supply its own implementation without touching collector code — the same
seam `ServerInventoryProvider` gives real-vs-fake collectors.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.domain.enums import ManagerType


class ManagerNotConfiguredError(Exception):
    """No connection details are configured for a manager type.

    Deliberately not an `app.errors.AppError` — those exist to become RFC
    9457 responses for API clients, and this is raised in a collector CLI
    process that never serves HTTP, so the collector's own top-level
    handling (log + non-zero exit) is what deals with it.
    """


@dataclass(frozen=True, slots=True)
class ManagerConnection:
    """Everything needed to reach one vendor manager.

    `endpoint` is a bare hostname or IP, never a URL — each vendor SDK
    builds its own URL, and a scheme here produces a mangled one (see
    `app.infrastructure.providers.ucs_manager.client._validate_endpoint`).
    """

    endpoint: str
    username: str
    password: str

    def __repr__(self) -> str:
        """Redacted, so a stray log line, traceback frame or debugger
        session can never print the password.
        """
        return (
            f"ManagerConnection(endpoint={self.endpoint!r}, "
            f"username={self.username!r}, password='***')"
        )


class CredentialResolver(Protocol):
    def resolve(self, manager_type: ManagerType) -> ManagerConnection:
        """Connection details for `manager_type`.

        Raises `ManagerNotConfiguredError` if any of the three values is
        missing — never returns a partially-populated `ManagerConnection`,
        because a blank password reaches the vendor as a real login
        attempt and fails as "bad credentials" rather than as the
        configuration error it actually is.
        """
        ...
