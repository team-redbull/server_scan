"""Async wrapper over `ucsmsdk`'s synchronous `UcsHandle`.

`ucsmsdk` (Cisco's official UCS Manager Python SDK, github.com/CiscoUcs/
ucsmsdk, `pip install ucsmsdk`) has no async support at all — `login`/
`logout`/`query_classid` are blocking HTTP calls over the UCS Manager XML
API. Every one of them is dispatched through `asyncio.to_thread` here so a
collector run never blocks the event loop the rest of this backend depends
on — the same "never block the loop" discipline
`app.infrastructure.mongodb`/`app.infrastructure.redis` already follow via
their own native async drivers, just substituted with a thread offload
since no async UCS SDK exists to reach for instead.

Confirmed directly against the installed `ucsmsdk==0.9.27` package source
(not just documentation):

  - `UcsHandle(ip, username, password, port=None, secure=None, proxy=None,
    timeout=None)`. `timeout` is urllib's, so it bounds each individual
    socket operation (connect, and each blocking read) — it is *not* a
    total-request or total-run deadline.
  - `endpoint` must be a bare hostname or IP. `UcsSession.__create_uri`
    builds `"%s://%s:%s" % (protocol, ip, port)` with `ip` interpolated
    raw, so a scheme or an embedded port produces a mangled URL
    ("https://https://host:443"). `_validate_endpoint` below rejects both
    up front rather than letting it fail as an opaque connection error.
  - `query_classid` returns a plain list (never `None`), `[]` when empty.
  - Exceptions come from two disjoint trees, both rooted at `Exception`:
    `UcsError` (with `UcsException`, `UcsValidationException`) and
    `UcsWrapperException` (with `UcsLoginError`, `UcsConnectionError`,
    `UcsOperationError`). Catching the two roots covers all six.
  - `login()` raises on bad credentials — it never returns a falsy value —
    so an authentication failure can't silently proceed as if connected.
  - `logout()` before a successful login is a no-op (`_logout` returns
    early when the session cookie is `None`), so calling it from a
    `finally` after a failed login costs nothing and sends no request.

Network-level failures are *not* part of either SDK exception tree:
`ucsdriver.post` re-raises urllib's errors untouched, and `URLError`/
`socket.timeout` are `OSError` subclasses. Every call below therefore
catches `OSError` alongside the SDK trees, so callers only ever see one
`UcsManagerConnectionError` regardless of which layer failed.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any
from urllib.parse import urlparse

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


def _validate_endpoint(endpoint: str) -> str:
    """`UcsHandle` wants a bare host or IP — see the module docstring on
    `__create_uri`. `Manager.endpoint` is a free-form `str`, so catch a
    scheme or an embedded port here with an actionable message instead of
    letting it surface as an unexplained connection failure.
    """
    candidate = endpoint.strip()
    if not candidate:
        raise ValueError("UCS Manager endpoint is empty.")
    if "://" in candidate:
        host = urlparse(candidate).hostname or ""
        raise ValueError(
            f"UCS Manager endpoint {endpoint!r} must be a bare hostname or IP, not a URL — "
            f"ucsmsdk builds the URL itself (use {host!r})."
        )
    if ":" in candidate and not candidate.startswith("["):  # not a bare IPv6 literal
        raise ValueError(
            f"UCS Manager endpoint {endpoint!r} must not include a port — "
            "ucsmsdk appends one itself (443 by default)."
        )
    return candidate


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
        self._handle = UcsHandle(
            _validate_endpoint(endpoint), username, password, timeout=timeout_seconds
        )
        # `ucsmsdk`'s own request/response XML dump, for seeing exactly
        # what came off the wire. Read from the environment rather than
        # threaded through every constructor, because it is a debugging
        # switch, not a property of a manager — `tools/run_collector.py
        # --debug-xml` sets it. Never on by default: the dump includes
        # full inventory payloads and would bury a real collector run.
        if os.environ.get("INVENTORY_UCS_DUMP_XML") == "1":
            self._handle.set_dump_xml()

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
        whole UCS domain.

        This is the only query shape the collector uses. Descendant
        objects (a server's management interface, its adapter host
        Ethernet interfaces) are fetched domain-wide too and joined
        client-side by DN prefix — see `provider.py`'s module docstring
        for why a per-server `configResolveChildren` is not merely slower
        but wrong for those classes.
        """
        try:
            result = await asyncio.to_thread(self._handle.query_classid, class_id)
        except (UcsError, UcsWrapperException) as exc:
            raise UcsManagerConnectionError(f"query_classid({class_id!r}) failed: {exc}") from exc
        except OSError as exc:
            raise UcsManagerConnectionError(
                f"query_classid({class_id!r}) could not reach {self._handle.ip}: {exc}"
            ) from exc
        return list(result) if result else []
