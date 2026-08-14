"""Async wrapper over `ucsmsdk`'s synchronous `UcsHandle`.

`ucsmsdk` (Cisco's official UCS Manager Python SDK, github.com/CiscoUcs/
ucsmsdk, `pip install ucsmsdk`) has no async support at all — `login`/
`logout`/`query_classid`/`query_children` are blocking HTTP calls over
the UCS Manager XML API. Every one of them is dispatched through
`asyncio.to_thread` here so a collector run never blocks the event loop
the rest of this backend depends on — the same "never block the loop"
discipline `app.infrastructure.mongodb`/`app.infrastructure.redis`
already follow via their own native async drivers, just substituted with
a thread offload since no async UCS SDK exists to reach for instead.

Confirmed directly against the installed `ucsmsdk==0.9.27` package
source (not just documentation) — `UcsHandle.query_classid` returns a
plain list of managed objects, and its exceptions are `ucsexception.
UcsError`/`UcsException` (protocol-level failures, e.g. a bad login) or
`ucsexception.UcsWrapperException`/`UcsLoginError`/`UcsConnectionError`
(SDK-level failures) — both hierarchies are caught here and normalized
into one `UcsManagerConnectionError` so `UcsManagerProvider` doesn't need
to know either exception tree.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog
from ucsmsdk.ucsexception import UcsError, UcsWrapperException
from ucsmsdk.ucshandle import UcsHandle

logger = structlog.get_logger(__name__)


class UcsManagerConnectionError(Exception):
    """Any failure talking to a UCS Manager domain: auth rejected, XML
    API error response, or a network-level failure reaching `endpoint`
    at all. Deliberately not an `app.errors.AppError` — see
    `app.domain.ports.credentials.CredentialNotFoundError`'s docstring
    for why collector-side errors don't go through the API's RFC 9457
    error model.
    """


class UcsManagerClient:
    """One instance per manager domain per collector run. Not pooled or
    reused across managers: `UcsHandle` isn't documented as safe for
    concurrent use from multiple tasks, and `asyncio.to_thread`'s
    one-call-at-a-time dispatch from a single client instance keeps every
    call to this handle sequential, matching that assumption.
    """

    def __init__(
        self, *, endpoint: str, username: str, password: str, timeout_seconds: float
    ) -> None:
        self._handle = UcsHandle(endpoint, username, password, timeout=timeout_seconds)

    async def login(self) -> None:
        try:
            await asyncio.to_thread(self._handle.login)
        except (UcsError, UcsWrapperException) as exc:
            raise UcsManagerConnectionError(f"Login to {self._handle.ip} failed: {exc}") from exc
        except OSError as exc:
            raise UcsManagerConnectionError(
                f"Could not reach UCS Manager at {self._handle.ip}: {exc}"
            ) from exc

    async def logout(self) -> None:
        # Best-effort: a failed logout (e.g. the connection already
        # dropped) must never mask whatever error the caller is already
        # handling — this is always called from a `finally` block.
        try:
            await asyncio.to_thread(self._handle.logout)
        except Exception as exc:
            logger.warning("ucs_manager.logout_failed", endpoint=self._handle.ip, error=str(exc))

    async def query_classid(self, class_id: str) -> list[Any]:
        """`configResolveClass` for every instance of `class_id` in the
        whole UCS domain — used for domain-wide queries (all compute
        blades, all rack units, all service profile templates), not for
        anything scoped under a specific server's DN (see
        `query_children` for that).
        """
        try:
            result = await asyncio.to_thread(self._handle.query_classid, class_id)
        except (UcsError, UcsWrapperException) as exc:
            raise UcsManagerConnectionError(f"query_classid({class_id!r}) failed: {exc}") from exc
        return list(result) if result else []

    async def query_children(self, *, in_dn: str, class_id: str) -> list[Any]:
        """`configResolveChildren` scoped under `in_dn`, filtered to
        `class_id`, `hierarchy=True` so it matches regardless of how many
        levels deep the real object sits under `in_dn` — used for
        per-server lookups (a blade/rack unit's management interface,
        adapter host Ethernet interfaces) whose exact intermediate parent
        MO wasn't confirmed against a live UCS Manager while building
        this (see `mapping.py`'s module docstring).
        """
        try:
            result = await asyncio.to_thread(
                self._handle.query_children, in_dn=in_dn, class_id=class_id, hierarchy=True
            )
        except (UcsError, UcsWrapperException) as exc:
            raise UcsManagerConnectionError(
                f"query_children(in_dn={in_dn!r}, class_id={class_id!r}) failed: {exc}"
            ) from exc
        return list(result) if result else []
