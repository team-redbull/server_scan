"""Async Redfish client for one BMC, on the `httpx` this project already
pins.

Written rather than taken from a library for reasons recorded in
docs/adr/0016-redfish-standalone-collector.md: the DMTF library hardcodes
`verify = False` and logs response headers unredacted (where the session
token lives), and `sushy` is sync-only and untyped. What is carried over
from `sushy` is its hard-won field knowledge, not its code — `Connection:
close`, split connect/read timeouts, and never retrying an `SSLError`.

The exception hierarchy is wider than the Cisco clients' single type on
purpose. Those deliberately collapse "rejected" and "unreachable"; here
the two must drive different behaviour, because retrying a rejected login
across an estate locks accounts.
"""

from __future__ import annotations

import asyncio
import random
import ssl
from types import TracebackType
from typing import Any, Self

import httpx
import structlog

from app.infrastructure.providers.redfish.targets import RedfishTarget

logger = structlog.get_logger(__name__)

_ODATA_ROOT = "/redfish/v1"
_SERVICE_ROOT = "/redfish/v1/"
_MAX_ATTEMPTS = 3
_RETRY_STATUSES = frozenset({429, 503})
# Redfish payloads are small and this runs on a LAN, so nothing is lost by
# capping the body — and an unbounded `.json()` on a wedged or hostile BMC
# takes the whole run's pod with it.
_MAX_RESPONSE_BYTES = 32 * 1024 * 1024

_TLS_VERSIONS = {
    "TLSv1": ssl.TLSVersion.TLSv1,
    "TLSv1_1": ssl.TLSVersion.TLSv1_1,
    "TLSv1_2": ssl.TLSVersion.TLSv1_2,
    "TLSv1_3": ssl.TLSVersion.TLSv1_3,
}


class RedfishError(Exception):
    """Any failure talking to one BMC."""


class RedfishUnreachableError(RedfishError):
    """DNS, refused, timed out, or a transport-level protocol failure.

    Retryable, and never counts toward a credential's failure budget.
    """


class RedfishTlsError(RedfishError):
    """Certificate verification or TLS handshake failure.

    Never retried: sushy's rule, and its reasoning is right — this is a
    configuration problem, not a transient one.
    """


class RedfishAuthError(RedfishError):
    """The BMC rejected the credential (401, or 403 on session creation).

    Never retried, and the only failure that counts toward the credential
    breaker and the run's authentication budget.
    """


class RedfishProtocolError(RedfishError):
    """Reachable and authenticated, but the response is unusable.

    Also raised for a service that is not conformant Redfish at all,
    which is what keeps a pre-Redfish BMC from producing a half-populated
    record fifteen requests later.
    """


class RedfishForbiddenError(RedfishError):
    """A resource returned 403 after a successful login.

    Deliberately *not* a `RedfishAuthError`: a ReadOnly BMC account —
    which this collector asks operators to use — legitimately gets 403 on
    some vendors' resources. Feeding those to the credential breaker
    would abort every run on a correctly-configured estate.
    """


def build_ssl_context(target: RedfishTarget, *, min_version: str) -> ssl.SSLContext | bool:
    """
    Build the TLS configuration for one BMC.

    Args:
        target (RedfishTarget): The host, carrying its own verification
            settings.
        min_version (str): Minimum TLS version name, e.g. `"TLSv1_2"`.

    Returns:
        ssl.SSLContext | bool: A configured context, or `False` for a host
            whose operator explicitly opted out of verification.
    """
    if not target.verify_tls:
        # The single opt-out in the codebase: per host, reason-gated at
        # load time, logged every run. Note ruff's S501 does NOT flag this
        # — it only matches a literal `verify=False` keyword argument, so
        # the moment the escape hatch exists the linter stops helping.
        # `test_verification_is_on_unless_a_host_opts_out` is the control.
        return False
    context = ssl.create_default_context(cafile=target.ca_bundle or None)
    context.minimum_version = _TLS_VERSIONS.get(min_version, ssl.TLSVersion.TLSv1_2)
    return context


def validate_odata_id(odata_id: object) -> str:
    """
    Check that a link the BMC handed us points back into its own tree.

    `@odata.id` is a relative URI by specification. An absolute one, or
    one climbing out with `..`, would retarget the next request — and
    since `X-Auth-Token` rides on every request, that hands a live session
    token to a host of the BMC's choosing.

    Args:
        odata_id (object): The `@odata.id` value as received.

    Returns:
        str: The validated path.

    Raises:
        RedfishProtocolError: If it is missing, not a string, not rooted
            at `/redfish/v1`, or contains a traversal segment.
    """
    if not isinstance(odata_id, str) or not odata_id:
        raise RedfishProtocolError(f"@odata.id is missing or not a string: {odata_id!r}")
    if not odata_id.startswith(_ODATA_ROOT):
        raise RedfishProtocolError(
            f"@odata.id {odata_id!r} is not a relative path under {_ODATA_ROOT} — refusing to "
            "follow it, since the session token travels with every request."
        )
    if ".." in odata_id.split("/"):
        raise RedfishProtocolError(f"@odata.id {odata_id!r} contains a traversal segment.")
    return odata_id


class RedfishClient:
    """
    One authenticated Redfish session against one BMC.

    Used as an async context manager so the session is always deleted:
    `__aenter__` probes the service root and logs in, `__aexit__` logs
    out. A client that exists is therefore authenticated, and no call site
    can forget to clean up.

    See docs/adr/0016-redfish-standalone-collector.md.
    """

    def __init__(
        self,
        *,
        target: RedfishTarget,
        connect_timeout: float,
        read_timeout: float,
        tls_min_version: str = "TLSv1_2",
        debug_http: bool = False,
    ) -> None:
        """
        Build a client for one BMC. No I/O happens here.

        Args:
            target (RedfishTarget): Host, credential and TLS settings.
            connect_timeout (float): Seconds to establish a connection.
            read_timeout (float): Seconds to wait for a response body.
            tls_min_version (str): Minimum TLS version name.
            debug_http (bool): Emit one redacted line per request.
        """
        self._target = target
        self._debug_http = debug_http
        self._token: str | None = None
        self._session_uri: str | None = None
        self.service_root: dict[str, Any] = {}
        self._client = httpx.AsyncClient(
            base_url=target.base_url,
            verify=build_ssl_context(target, min_version=tls_min_version),
            timeout=httpx.Timeout(
                connect=connect_timeout, read=read_timeout, write=read_timeout, pool=connect_timeout
            ),
            # `Connection: close` is sent for the BMC's benefit — sushy's
            # field studies found BMCs that choke on persistent
            # connections — and the zero keepalive pool is what makes our
            # side actually honour it rather than merely ask.
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
            headers={
                "OData-Version": "4.0",
                "Accept": "application/json",
                "Connection": "close",
            },
            # A 3xx is treated as an error, never followed: it is the
            # other way an untrusted device retargets our next request,
            # and httpx cannot know a custom `X-Auth-Token` is sensitive
            # the way it knows `Authorization` is.
            follow_redirects=False,
        )

    async def __aenter__(self) -> Self:
        """
        Probe the service root, confirm it is conformant Redfish, and open
        a session.

        Returns:
            Self: The authenticated client.

        Raises:
            RedfishUnreachableError: If the BMC cannot be reached.
            RedfishTlsError: If its certificate cannot be verified.
            RedfishProtocolError: If it is not conformant Redfish.
            RedfishAuthError: If the credential is rejected.
        """
        try:
            self.service_root = await self._request("GET", _SERVICE_ROOT, authenticated=False)
            self._assert_conformant(self.service_root)
            await self._login()
        except BaseException:
            await self._client.aclose()
            raise
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """
        Delete the session and close the transport. Never raises.

        Shielded from cancellation: without that, a cancelled task's
        `await` on the logout raises immediately, the DELETE is never
        sent, and the leaked session counts against a BMC session cap that
        is often as low as 16.
        """
        try:
            if self._session_uri is not None:
                await asyncio.shield(asyncio.wait_for(self._logout(), timeout=10.0))
        # Teardown must never mask the error that caused it.
        except Exception as exc_info:
            logger.warning("redfish.logout_failed", host=self._target.host, error=str(exc_info))
        except asyncio.CancelledError:
            logger.warning("redfish.logout_cancelled", host=self._target.host)
        finally:
            await self._client.aclose()

    def _assert_conformant(self, root: dict[str, Any]) -> None:
        """
        Reject a service that is not conformant Redfish, before any
        credential is sent.

        Pre-Redfish services (notably HPE iLO 4) answer `/redfish/v1` with
        a dotted `@odata.type` and different property spellings. Without
        this check they produce a half-populated record or a failure deep
        in the traversal; with it they fail legibly. Stated by shape, not
        by vendor, so any equally divergent BMC fails the same way.

        Args:
            root (dict[str, Any]): The service root payload.

        Raises:
            RedfishProtocolError: If the payload is not conformant.
        """
        odata_type = str(root.get("@odata.type", ""))
        if "RedfishVersion" not in root or ".v1_" not in odata_type:
            raise RedfishProtocolError(
                f"{self._target.host} does not answer /redfish/v1 with a conformant Redfish "
                f"service root (@odata.type={odata_type or 'missing'!r}, "
                f"RedfishVersion={root.get('RedfishVersion', 'missing')!r}). Pre-Redfish "
                "services such as HPE iLO 4 are out of scope."
            )

    def _sessions_uri(self) -> str:
        """
        Where to POST to create a session.

        Never hardcoded: DSP0266 §13.3.4.1 says find it at
        `SessionService.Sessions` or `ServiceRoot.Links.Sessions`.

        Returns:
            str: The sessions collection path.

        Raises:
            RedfishProtocolError: If the service root advertises neither.
        """
        links = self.service_root.get("Links", {})
        for candidate in (
            links.get("Sessions", {}).get("@odata.id") if isinstance(links, dict) else None,
            self.service_root.get("SessionService", {}).get("@odata.id"),
        ):
            if candidate:
                return validate_odata_id(candidate)
        raise RedfishProtocolError(
            f"{self._target.host} advertises no Sessions collection in its service root."
        )

    async def _login(self) -> None:
        """
        Create a session and capture its token and location.

        Raises:
            RedfishAuthError: If the credential is rejected.
            RedfishProtocolError: If the response carries no token.
        """
        # Posted exactly as advertised — DSP0266 places no requirement on
        # a trailing slash either way, and appending one unconditionally
        # broke real hardware: confirmed against a live BMC that answers
        # its Sessions collection at an exact path and 404s the same URI
        # with a trailing slash appended. The CI fixture never caught this
        # because its own routing matches by prefix (`str.startswith`),
        # tolerating exactly the mistake real hardware does not.
        response = await self._send(
            "POST",
            self._sessions_uri(),
            authenticated=False,
            json={
                "UserName": self._target.credential.username,
                "Password": self._target.credential.password,
            },
        )
        if response.status_code in (401, 403):
            raise RedfishAuthError(
                f"{self._target.host} rejected credential "
                f"{self._target.credential.name!r} ({response.status_code})"
            )
        if response.status_code >= 400:
            # Logged with the status rather than assumed: some BMCs are
            # reported to answer a bad password with 400, and only real
            # hardware settles that.
            raise RedfishProtocolError(
                f"{self._target.host} refused session creation with HTTP {response.status_code}"
            )
        token = response.headers.get("X-Auth-Token")
        if not token:
            raise RedfishProtocolError(
                f"{self._target.host} created a session but returned no X-Auth-Token."
            )
        self._token = token
        self._session_uri = response.headers.get("Location") or None

    async def _logout(self) -> None:
        """Delete this client's session. Best effort."""
        if self._session_uri is None:
            return
        path = self._session_uri
        if path.startswith("http"):
            path = httpx.URL(path).path
        await self._send("DELETE", path, authenticated=True)

    async def get(self, path: str) -> dict[str, Any]:
        """
        Fetch one resource.

        Args:
            path (str): A validated `@odata.id`.

        Returns:
            dict[str, Any]: The parsed payload.

        Raises:
            RedfishError: Per the module's exception hierarchy.
        """
        return await self._request("GET", path, authenticated=True)

    async def get_collection(self, path: str) -> list[dict[str, Any]]:
        """
        Fetch every member of a collection, following pagination.

        `Members@odata.count` is deliberately ignored: DSP0266 defines it
        as the total across *all* pages, so comparing it to this page's
        length is meaningless, and trusting it in place of following
        `Members@odata.nextLink` silently truncates the fleet.

        A member that arrives already expanded is used as-is; one that is
        a bare link is fetched. That single branch makes `$expand` on and
        off the same code path, and also handles a legally link-only
        member inside an expanded collection.

        Args:
            path (str): The collection's `@odata.id`.

        Returns:
            list[dict[str, Any]]: Every member's payload.

        Raises:
            RedfishError: Per the module's exception hierarchy.
        """
        members: list[dict[str, Any]] = []
        next_path: str | None = validate_odata_id(path)
        while next_path:
            page = await self.get(next_path)
            for member in page.get("Members", []) or []:
                if not isinstance(member, dict):
                    continue
                if "@odata.type" in member:
                    members.append(member)
                else:
                    members.append(await self.get(validate_odata_id(member.get("@odata.id"))))
            raw_next = page.get("Members@odata.nextLink")
            next_path = validate_odata_id(raw_next) if raw_next else None
        return members

    async def _request(self, method: str, path: str, *, authenticated: bool) -> dict[str, Any]:
        """
        Issue a request and parse its JSON body.

        Args:
            method (str): HTTP method.
            path (str): Request path.
            authenticated (bool): Whether to send the session token.

        Returns:
            dict[str, Any]: The parsed payload.

        Raises:
            RedfishAuthError: On a 401.
            RedfishForbiddenError: On a 403 for a resource.
            RedfishProtocolError: On any other error status, a redirect,
                an oversized body, or unparseable JSON.
        """
        response = await self._send(method, path, authenticated=authenticated)
        if response.status_code == 401:
            raise RedfishAuthError(f"{self._target.host} returned 401 for {path}")
        if response.status_code == 403:
            raise RedfishForbiddenError(f"{self._target.host} returned 403 for {path}")
        if 300 <= response.status_code < 400:
            raise RedfishProtocolError(
                f"{self._target.host} redirected {path} (HTTP {response.status_code}); "
                "redirects are never followed, since the session token travels with the request."
            )
        if response.status_code >= 400:
            raise RedfishProtocolError(
                f"{self._target.host} returned HTTP {response.status_code} for {path}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise RedfishProtocolError(f"{self._target.host} returned non-JSON for {path}") from exc
        if not isinstance(payload, dict):
            raise RedfishProtocolError(
                f"{self._target.host} returned a non-object payload for {path}"
            )
        return payload

    async def _send(
        self, method: str, path: str, *, authenticated: bool, json: dict[str, str] | None = None
    ) -> httpx.Response:
        """
        Send one request, retrying only transport-level failures.

        Never retries a 4xx — a rejected credential retried across an
        estate is what locks accounts — and never retries an `SSLError`,
        which is a configuration problem rather than a transient one.

        Args:
            method (str): HTTP method.
            path (str): Request path.
            authenticated (bool): Whether to send the session token.
            json (dict[str, str] | None): Request body, if any.

        Returns:
            httpx.Response: The response, whatever its status.

        Raises:
            RedfishTlsError: On a certificate or handshake failure.
            RedfishUnreachableError: If the host stays unreachable.
        """
        headers = {"X-Auth-Token": self._token} if authenticated and self._token else None
        last: Exception | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                response = await self._client.request(method, path, headers=headers, json=json)
                self._trace(method, path, response.status_code)
                if response.status_code in _RETRY_STATUSES and attempt < _MAX_ATTEMPTS:
                    await self._backoff(attempt, response.headers.get("Retry-After"))
                    continue
                await self._guard_size(response, path)
                return response
            except httpx.ConnectError as exc:
                if isinstance(exc.__cause__, ssl.SSLError):
                    raise RedfishTlsError(
                        f"TLS verification failed for {self._target.host}: {exc}. Point "
                        "INVENTORY_REDFISH_CA_BUNDLE at the issuing CA, or set "
                        "`verify_tls = false` with a reason for this host."
                    ) from exc
                last = exc
            except (httpx.TimeoutException, httpx.RemoteProtocolError, httpx.ReadError) as exc:
                last = exc
            if attempt < _MAX_ATTEMPTS:
                await self._backoff(attempt, None)
        raise RedfishUnreachableError(f"Could not reach {self._target.host}: {last}")

    async def _guard_size(self, response: httpx.Response, path: str) -> None:
        """
        Refuse a response too large to hold in memory.

        Bounds a merely-buggy BMC as much as a hostile one: an
        unbounded body on one host loses the whole run's pod, not just
        that server. Checked after reading because httpx has already
        decompressed by then, which is what makes the cap meaningful
        rather than advisory — a `Content-Length` check alone is
        defeated by compression.

        Args:
            response (httpx.Response): The response to check.
            path (str): Request path, for the message.

        Raises:
            RedfishProtocolError: If the body exceeds the cap.
        """
        if len(response.content) > _MAX_RESPONSE_BYTES:
            raise RedfishProtocolError(
                f"{self._target.host} returned {len(response.content)} bytes for {path}, "
                f"over the {_MAX_RESPONSE_BYTES} byte cap."
            )

    async def _backoff(self, attempt: int, retry_after: str | None) -> None:
        """
        Wait before retrying a transport failure.

        Args:
            attempt (int): 1-based attempt just completed.
            retry_after (str | None): The response's `Retry-After`, if any.
        """
        if retry_after and retry_after.isdigit():
            await asyncio.sleep(min(float(retry_after), 30.0))
            return
        await asyncio.sleep(min(2.0**attempt, 8.0) + random.uniform(0, 0.5))  # noqa: S311

    def _trace(self, method: str, path: str, status: int) -> None:
        """
        Emit one debug line per request.

        Method, path and status only — never a header, never a body. The
        session exchange is excluded outright rather than redacted,
        because that one request carries the password and its response
        carries the token, and a redactor that must be perfect is a worse
        design than never formatting the value at all.

        Args:
            method (str): HTTP method.
            path (str): Request path.
            status (int): Response status code.
        """
        if not self._debug_http or "SessionService" in path or "Sessions" in path:
            return
        logger.debug(
            "redfish.http", host=self._target.host, method=method, path=path, status=status
        )
