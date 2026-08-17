"""Async wrapper over `ucscsdk`'s synchronous `UcscHandle`.

See docs/cisco-collectors.md, "SDK behaviour, sessions and timeouts".
"""

from __future__ import annotations

import asyncio
import os
from typing import Any
from urllib.parse import urlparse

import structlog
from ucscsdk.ucscexception import UcscError, UcscWrapperException
from ucscsdk.ucschandle import UcscHandle

logger = structlog.get_logger(__name__)


class UcsCentralConnectionError(Exception):
    """
    Any failure talking to UCS Central: auth rejected, an XML API error
    response, a network-level failure, or the timeout this module imposes
    because the SDK offers none.

    See docs/cisco-collectors.md, "SDK behaviour, sessions and timeouts".
    """


def _validate_endpoint(endpoint: str) -> str:
    """
    Check that an endpoint is the bare host or IP `UcscHandle` requires.

    See docs/cisco-collectors.md, "SDK behaviour, sessions and timeouts".

    Args:
        endpoint (str): The configured UCS Central address.

    Returns:
        str: The endpoint, stripped of surrounding whitespace.

    Raises:
        ValueError: If the endpoint is empty, carries a URL scheme, or
            includes a port.
    """
    candidate = endpoint.strip()
    if not candidate:
        raise ValueError("UCS Central endpoint is empty.")
    if "://" in candidate:
        host = urlparse(candidate).hostname or ""
        raise ValueError(
            f"UCS Central endpoint {endpoint!r} must be a bare hostname or IP, not a URL — "
            f"ucscsdk builds the URL itself (use {host!r})."
        )
    if ":" in candidate and not candidate.startswith("["):  # not a bare IPv6 literal
        raise ValueError(
            f"UCS Central endpoint {endpoint!r} must not include a port — "
            "ucscsdk hardcodes 443 and rejects anything else."
        )
    return candidate


class UcsCentralClient:
    """
    One UCS Central session, held for the length of a single collector run.

    Not pooled or reused across runs.

    See docs/cisco-collectors.md, "SDK behaviour, sessions and timeouts".
    """

    def __init__(
        self, *, endpoint: str, username: str, password: str, timeout_seconds: float
    ) -> None:
        """
        Build a handle for one UCS Central endpoint.

        Args:
            endpoint (str): Bare hostname or IP of the UCS Central instance.
            username (str): Login name.
            password (str): Login password.
            timeout_seconds (float): Deadline applied to each SDK call.

        Raises:
            ValueError: If `endpoint` is not a bare hostname or IP.
        """
        self._handle = UcscHandle(_validate_endpoint(endpoint), username, password)
        self._timeout_seconds = timeout_seconds
        if os.environ.get("INVENTORY_UCS_DUMP_XML") == "1":
            self._handle.set_dump_xml()

    async def _with_timeout(self, func: Any, *args: Any, what: str) -> Any:
        """
        Run a blocking SDK call in a worker thread under a deadline.

        See docs/cisco-collectors.md, "SDK behaviour, sessions and timeouts".

        Args:
            func (Any): The blocking SDK callable to dispatch.
            *args (Any): Positional arguments forwarded to `func`.
            what (str): Human-readable description of the call, used in
                error messages.

        Returns:
            Any: Whatever `func` returned.

        Raises:
            UcsCentralConnectionError: On timeout, any SDK exception, or any
                network-level `OSError`.
        """
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(func, *args), timeout=self._timeout_seconds
            )
        except TimeoutError as exc:
            raise UcsCentralConnectionError(
                f"{what} timed out after {self._timeout_seconds}s "
                f"(ucscsdk has no timeout of its own; this deadline is imposed by the collector)."
            ) from exc
        except (UcscError, UcscWrapperException) as exc:
            raise UcsCentralConnectionError(f"{what} failed: {exc}") from exc
        except OSError as exc:
            raise UcsCentralConnectionError(
                f"{what} could not reach UCS Central at {self._handle.ip}: {exc}"
            ) from exc

    async def login(self) -> None:
        """
        Open the UCS Central session.

        Raises:
            UcsCentralConnectionError: If authentication or the connection
                fails.
        """
        await self._with_timeout(self._handle.login, what=f"Login to {self._handle.ip}")

    async def logout(self) -> None:
        """
        Close the UCS Central session, best-effort.

        Always safe to call from a `finally` block: a failed logout is
        logged and swallowed so it can never mask the error the caller is
        already handling, and a logout before a successful login is a no-op.

        See docs/cisco-collectors.md, "SDK behaviour, sessions and timeouts".
        """
        try:
            await self._with_timeout(self._handle.logout, what="Logout")
        except Exception as exc:
            logger.warning("ucs_central.logout_failed", endpoint=self._handle.ip, error=str(exc))

    async def query_classid(self, class_id: str) -> list[Any]:
        """
        Resolve every instance of `class_id` across every registered domain.

        See docs/cisco-collectors.md, "UCS Central domain discovery and
        pruning" for why no server-side `filter_str` is used.

        Args:
            class_id (str): The managed-object class to resolve, e.g.
                `"computeSystem"`.

        Returns:
            list[Any]: Every matching managed object, empty if none.

        Raises:
            UcsCentralConnectionError: On timeout, SDK error, or network
                failure.
        """
        # ponytail: one filter, applied once, in the collector. Push the
        # pattern down to filter_str only if payload size ever actually
        # hurts — at 10k servers this is a few MB per run.
        result = await self._with_timeout(
            self._handle.query_classid, class_id, what=f"query_classid({class_id!r})"
        )
        return list(result) if result else []
