"""`app.infrastructure.providers.ucs_manager.client`.

The point of `UcsManagerClient` is that callers see exactly one error type
no matter which layer failed — the SDK's two exception trees, or a raw
network error from urllib underneath them. `ucsmsdk` itself is not
exercised here (that needs a live domain or UCSPE); the real `UcsHandle`
is swapped for a stub after construction so the wrapper's own translation
and endpoint validation are what's under test.
"""

from __future__ import annotations

from typing import Any
from urllib.error import URLError

import pytest
from ucsmsdk.ucsexception import UcsException, UcsLoginError

from app.infrastructure.providers.ucs_manager.client import (
    UcsManagerClient,
    UcsManagerConnectionError,
    _validate_endpoint,
)

pytestmark = pytest.mark.unit


class StubHandle:
    def __init__(self, *, error: Exception | None = None, result: Any = None) -> None:
        self.ip = "ucsm.lab.example.com"
        self._error = error
        self._result = result
        self.calls: list[str] = []

    def _maybe_raise(self) -> None:
        if self._error is not None:
            raise self._error

    def login(self) -> bool:
        self.calls.append("login")
        self._maybe_raise()
        return True

    def logout(self) -> bool:
        self.calls.append("logout")
        self._maybe_raise()
        return True

    def query_classid(self, class_id: str) -> Any:
        self.calls.append(f"query_classid:{class_id}")
        self._maybe_raise()
        return self._result


def _client(handle: StubHandle) -> UcsManagerClient:
    client = UcsManagerClient(
        endpoint="ucsm.lab.example.com", username="admin", password="secret", timeout_seconds=5.0
    )
    client._handle = handle  # type: ignore[assignment]
    return client


class TestValidateEndpoint:
    def test_accepts_a_bare_host(self) -> None:
        assert _validate_endpoint("  ucsm.lab.example.com  ") == "ucsm.lab.example.com"

    def test_accepts_a_bare_ip(self) -> None:
        assert _validate_endpoint("10.1.2.3") == "10.1.2.3"

    @pytest.mark.parametrize(
        "endpoint",
        ["https://ucsm.lab.example.com", "http://10.1.2.3"],
    )
    def test_rejects_a_url(self, endpoint: str) -> None:
        """`ucsmsdk` interpolates this raw into "%s://%s:%s", so a scheme
        yields "https://https://host:443" and an opaque failure.
        """
        with pytest.raises(ValueError, match="bare hostname or IP"):
            _validate_endpoint(endpoint)

    def test_rejects_an_embedded_port(self) -> None:
        with pytest.raises(ValueError, match="must not include a port"):
            _validate_endpoint("ucsm.lab.example.com:443")

    def test_rejects_empty(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            _validate_endpoint("   ")


class TestErrorTranslation:
    async def test_login_translates_sdk_errors(self) -> None:
        client = _client(StubHandle(error=UcsException(551, "Authentication failed")))
        with pytest.raises(UcsManagerConnectionError, match="Login to"):
            await client.login()

    async def test_login_translates_wrapper_errors(self) -> None:
        """`UcsLoginError` is under `UcsWrapperException`, a tree disjoint
        from `UcsError` — both roots have to be caught.
        """
        client = _client(StubHandle(error=UcsLoginError("Not a supported server.")))
        with pytest.raises(UcsManagerConnectionError):
            await client.login()

    async def test_login_translates_network_errors(self) -> None:
        client = _client(StubHandle(error=URLError("connection refused")))
        with pytest.raises(UcsManagerConnectionError, match="Could not reach"):
            await client.login()

    async def test_query_translates_sdk_errors(self) -> None:
        client = _client(StubHandle(error=UcsException(105, "class not found")))
        with pytest.raises(UcsManagerConnectionError, match="query_classid"):
            await client.query_classid("computeBlade")

    @pytest.mark.parametrize(
        "error",
        [URLError("connection reset"), TimeoutError("timed out"), ConnectionResetError()],
    )
    async def test_query_translates_network_errors(self, error: Exception) -> None:
        """`ucsdriver.post` re-raises urllib's errors untouched, and these
        are all `OSError` subclasses — they are *not* part of either SDK
        exception tree, so a mid-collection network drop would otherwise
        escape as a raw `OSError`.
        """
        client = _client(StubHandle(error=error))
        with pytest.raises(UcsManagerConnectionError, match="could not reach"):
            await client.query_classid("computeBlade")

    async def test_logout_never_raises(self) -> None:
        """Always called from a `finally` — it must not mask the error the
        caller is already handling.
        """
        client = _client(StubHandle(error=URLError("already disconnected")))
        await client.logout()


class TestQueryResults:
    async def test_returns_a_list(self) -> None:
        client = _client(StubHandle(result=[1, 2, 3]))
        assert await client.query_classid("computeBlade") == [1, 2, 3]

    @pytest.mark.parametrize("result", [None, []])
    async def test_empty_results_become_an_empty_list(self, result: Any) -> None:
        client = _client(StubHandle(result=result))
        assert await client.query_classid("computeBlade") == []
