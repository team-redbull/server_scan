"""The collector credential seam.

`Manager.credential_ref`/`Manager.bmc_credential_ref` (`app.domain.models.
manager`) are opaque names, never plaintext values — "no credentials in
source" per the platform spec. Something still has to turn a name into a
real username/password at the moment a collector actually connects, and
that's what `CredentialResolver` is: a `Protocol`, not a concrete class,
so a real collector's dependency is "give me a resolver" rather than
"read this specific secret store" — production wiring can swap the
implementation (Kubernetes Secret volume, Vault, whatever the deployment
target actually uses) without touching collector code, the same seam
`ServerInventoryProvider` (`app.domain.ports.provider`) already gives
real-vs-fake collectors.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class CredentialNotFoundError(Exception):
    """`credential_ref` doesn't resolve to anything. Deliberately not an
    `app.errors.AppError` — those exist to become RFC 9457 responses for
    API clients, and this is raised in a collector CLI process that never
    serves HTTP, so the collector's own top-level error handling (log +
    non-zero exit for that manager, per `tools.run_collector`) is what
    handles it, not `app.exception_handlers`.
    """


@dataclass(frozen=True, slots=True)
class ManagerCredentials:
    username: str
    password: str


class CredentialResolver(Protocol):
    async def resolve(self, credential_ref: str) -> ManagerCredentials:
        """Look up the real credentials a `credential_ref` name points to.

        Raises `CredentialNotFoundError` if `credential_ref` doesn't
        resolve to anything — never returns a partial/empty
        `ManagerCredentials`.
        """
        ...
