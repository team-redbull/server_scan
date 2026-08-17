"""Async wrapper over `ucsmsdk`'s synchronous `UcsHandle`.

See docs/cisco-collectors.md, "SDK behaviour, sessions and timeouts".
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
    """
    Any failure talking to a UCS Manager domain.

    Covers rejected credentials, an XML API error response, and a
    network-level failure reaching the endpoint at all.

    See docs/cisco-collectors.md, "SDK behaviour, sessions and timeouts".
    """


def _validate_endpoint(endpoint: str) -> str:
    """
    Check that an endpoint is the bare host or IP `UcsHandle` expects.

    Args:
        endpoint (str): Hostname or IP address, without scheme or port.

    Returns:
        str: The endpoint, stripped of surrounding whitespace.

    Raises:
        ValueError: If the endpoint is empty, carries a URL scheme, or
            includes a port.

    See docs/cisco-collectors.md, "SDK behaviour, sessions and timeouts".
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
    """
    One UCS Manager session, for one domain, for one collector run.

    Not pooled or reused across domains.

    See docs/cisco-collectors.md, "SDK behaviour, sessions and timeouts".
    """

    def __init__(
        self, *, endpoint: str, username: str, password: str, timeout_seconds: float
    ) -> None:
        """
        Build a client bound to one UCS Manager domain.

        Args:
            endpoint (str): Bare hostname or IP of the domain.
            username (str): Login user.
            password (str): Login password.
            timeout_seconds (float): Per-socket-operation timeout, not a
                total-request or total-run deadline.

        Raises:
            ValueError: If `endpoint` is not a bare hostname or IP.
        """
        self._handle = UcsHandle(
            _validate_endpoint(endpoint), username, password, timeout=timeout_seconds
        )
        if os.environ.get("INVENTORY_UCS_DUMP_XML") == "1":
            self._handle.set_dump_xml()

    async def login(self) -> None:
        """
        Open a session against the domain.

        Raises:
            UcsManagerConnectionError: If the credentials are rejected, the
                XML API returns an error, or the host is unreachable.
        """
        try:
            await asyncio.to_thread(self._handle.login)
        except (UcsError, UcsWrapperException) as exc:
            raise UcsManagerConnectionError(f"Login to {self._handle.ip} failed: {exc}") from exc
        except OSError as exc:
            raise UcsManagerConnectionError(
                f"Could not reach UCS Manager at {self._handle.ip}: {exc}"
            ) from exc

    async def logout(self) -> None:
        """
        Close the session, best-effort.

        Never raises. Calling it before a successful login is a no-op that
        sends no request.

        See docs/cisco-collectors.md, "SDK behaviour, sessions and timeouts".
        """
        try:
            await asyncio.to_thread(self._handle.logout)
        except Exception as exc:
            logger.warning("ucs_manager.logout_failed", endpoint=self._handle.ip, error=str(exc))

    async def query_classid(self, class_id: str) -> list[Any]:
        """
        Resolve every instance of one MO class in the whole domain.

        Args:
            class_id (str): MO class name, e.g. "computeBlade".

        Returns:
            list[Any]: The matching `ucsmsdk` MOs, empty when none match.

        Raises:
            UcsManagerConnectionError: On an XML API error or a network
                failure.

        See docs/cisco-collectors.md, "Shared object model and DN joins".
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
