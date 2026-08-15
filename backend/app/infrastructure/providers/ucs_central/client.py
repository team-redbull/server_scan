"""Async wrapper over `ucscsdk`'s synchronous `UcscHandle`.

The UCS Central counterpart to `app.infrastructure.providers.ucs_manager.
client`. Same shape and the same "never block the event loop" discipline —
every blocking call goes through `asyncio.to_thread` — but `ucscsdk` is a
*separate* package from `ucsmsdk` with its own handle, its own exception
tree and its own MO tree, so this is a sibling rather than a subclass.

Confirmed directly against the installed `ucscsdk==0.9.0.10` package
source (not documentation):

  - `UcscHandle(ip, username, password, port=443, proxy=None)`. Note what
    is **missing**: there is no `timeout` parameter. `ucscsession.post`
    calls `ucscdriver.post(uri, data, read)` without forwarding one, so
    `urlopen` runs with `timeout=None` and a wedged Central would block
    forever. `_with_timeout` below is the compensating control, since
    there is no SDK knob to set.
  - `port` must be 443 — `__create_uri` raises for any other value — so
    unlike `ucsmsdk` there is nothing to configure and an endpoint with an
    embedded port is always wrong.
  - `endpoint` must be a bare hostname or IP: `__create_uri` builds
    `"%s://%s%s%s" % ("https", ip, ":", port)` with `ip` interpolated raw,
    so a scheme produces "https://https://host:443".
  - `query_classid(class_id=None, filter_str=None, hierarchy=False,
    need_response=False, dme='central-mgr')` returns a list, `[]` when
    empty.
  - Exceptions come from two disjoint trees, both rooted at `Exception`:
    `UcscError` (with `UcscException`, `UcscValidationException`) and
    `UcscWrapperException` (with `UcscLoginError`, `UcscConnectionError`,
    `UcscOperationError`). Catching the two roots covers all six — the
    same split `ucsmsdk` uses, with a `c` in the names.
  - `logout()` before a successful login is a no-op, so calling it from a
    `finally` after a failed login costs nothing.

Network-level failures are not part of either SDK exception tree:
`ucscdriver.post` re-raises urllib's errors untouched, and `URLError`/
`socket.timeout` are `OSError` subclasses. Every call below therefore
catches `OSError` alongside the SDK trees, so callers only ever see one
`UcsCentralConnectionError`.
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
    """Any failure talking to UCS Central: auth rejected, XML API error
    response, a network-level failure, or the timeout this module imposes
    because the SDK offers none. Deliberately not an `app.errors.AppError`
    — see `app.domain.ports.credentials.CredentialNotFoundError`'s
    docstring for why collector-side errors don't go through the API's
    RFC 9457 error model.
    """


def _validate_endpoint(endpoint: str) -> str:
    """`UcscHandle` wants a bare host or IP — see the module docstring on
    `__create_uri`.
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
    """One instance per collector run. Not pooled or reused: `UcscHandle`
    isn't documented as safe for concurrent use from multiple tasks, and
    `asyncio.to_thread`'s one-call-at-a-time dispatch from a single client
    instance keeps every call to this handle sequential.
    """

    def __init__(
        self, *, endpoint: str, username: str, password: str, timeout_seconds: float
    ) -> None:
        self._handle = UcscHandle(_validate_endpoint(endpoint), username, password)
        self._timeout_seconds = timeout_seconds
        # `ucscsdk`'s own request/response XML dump. Shares
        # `INVENTORY_UCS_DUMP_XML` with the UCS Manager client (set by
        # `tools/run_collector.py --debug-xml`) — one switch for "show me
        # the Cisco XML", whichever collector is running.
        if os.environ.get("INVENTORY_UCS_DUMP_XML") == "1":
            self._handle.set_dump_xml()

    async def _with_timeout(self, func: Any, *args: Any, what: str) -> Any:
        """Run a blocking SDK call in a worker thread under a deadline.

        `asyncio.wait_for` cancels the *await*, not the thread — a timed-out
        call leaves its worker blocked in `urlopen` until the OS gives up,
        so the thread leaks for the rest of the process. That is acceptable
        precisely here and nowhere else: a collector run is a short-lived
        CronJob process that exits soon after, and the CronJob's own
        `activeDeadlineSeconds` is the outer backstop. The alternative —
        no deadline at all, which is what the SDK gives you — is a
        collector that hangs until Kubernetes kills it with no logged
        reason.
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
        await self._with_timeout(self._handle.login, what=f"Login to {self._handle.ip}")

    async def logout(self) -> None:
        # Best-effort: a failed logout must never mask whatever error the
        # caller is already handling — this is always called from
        # `finally`.
        try:
            await self._with_timeout(self._handle.logout, what="Logout")
        except Exception as exc:
            logger.warning("ucs_central.logout_failed", endpoint=self._handle.ip, error=str(exc))

    async def query_classid(self, class_id: str) -> list[Any]:
        """`configResolveClass` for every instance of `class_id` across
        *every registered domain* — that is the whole point of querying
        Central rather than each UCS Manager in turn.

        No `filter_str` is passed even though `ucscsdk` supports one
        (`'(name, "ocp.*", type="re")'`). The name a server is filtered on
        lives on its `lsServer` service profile, not on the compute MO, so
        a server-side filter could only narrow one of the six queries
        while every join still needs the full compute inventory — and it
        would put a second, subtly different copy of "which servers are
        mine" next to `tools.run_collector._NameFilteredProvider`, which
        already applies `INVENTORY_COLLECTOR_NAME_PATTERN` for every
        vendor.
        # ponytail: one filter, applied once, in the collector. Push the
        # pattern down to filter_str only if payload size ever actually
        # hurts — at 10k servers this is a few MB per run.
        """
        result = await self._with_timeout(
            self._handle.query_classid, class_id, what=f"query_classid({class_id!r})"
        )
        return list(result) if result else []
