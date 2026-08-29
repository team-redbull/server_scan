"""`app.infrastructure.providers.intersight.client`.

Transport behaviour only — signing has its own module and its own test.
What matters here is that paging terminates and is complete, that a
throttled tenant is waited out rather than abandoned, and that each
failure an operator can actually fix produces a message naming the fix.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.infrastructure.providers.intersight.client import (
    IntersightAuthError,
    IntersightClient,
    IntersightForbiddenError,
    IntersightProtocolError,
    IntersightThrottledError,
    IntersightUnreachableError,
    validate_endpoint,
)

pytestmark = pytest.mark.unit


def _pem() -> str:
    """
    A throwaway signing key.

    Returns:
        str: PEM text.
    """
    return (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
        .decode()
    )


_KEY = _pem()


def _client(handler, **kwargs: Any) -> IntersightClient:  # type: ignore[no-untyped-def]
    """
    A client wired to a scripted transport.

    Args:
        handler: An `httpx.MockTransport` request handler.
        **kwargs: Overrides for the client constructor.

    Returns:
        IntersightClient: The client under test.
    """
    return IntersightClient(
        endpoint="intersight.com",
        key_id="a/b/c",
        private_key_pem=_KEY,
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def _page(rows: list[dict[str, Any]]) -> httpx.Response:
    """
    A successful list response.

    Args:
        rows (list[dict[str, Any]]): The `Results` payload.

    Returns:
        httpx.Response: A 200 carrying them.
    """
    return httpx.Response(200, json={"Count": len(rows), "Results": rows})


# --- paging -----------------------------------------------------------


@pytest.mark.asyncio
async def test_paging_walks_every_page_and_stops_on_a_short_one() -> None:
    """A full page implies another may follow; a short page ends it.

    Also pins that `$skip` advances — a client that resent `$skip=0`
    would loop forever against a fleet larger than one page.
    """
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params.get("$skip"))
        skip = int(request.url.params.get("$skip"))
        if skip == 0:
            return _page([{"Moid": "a"}, {"Moid": "b"}])
        return _page([{"Moid": "c"}])

    client = _client(handler, page_size=2)
    rows = [row async for row in client.list_all("compute/PhysicalSummaries")]
    await client.aclose()

    assert [row["Moid"] for row in rows] == ["a", "b", "c"]
    assert seen == ["0", "2"]


@pytest.mark.asyncio
async def test_every_page_is_ordered_by_moid() -> None:
    """`$top`/`$skip` is the only paging mechanism the API offers, so an
    unordered result set can skip or repeat rows between pages.
    """
    captured: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.url.params))
        return _page([])

    client = _client(handler)
    _ = [row async for row in client.list_all("compute/PhysicalSummaries", select="Moid")]
    await client.aclose()

    assert captured["$orderby"] == "Moid"
    assert captured["$select"] == "Moid"


@pytest.mark.asyncio
async def test_a_null_results_field_is_an_empty_page_not_an_error() -> None:
    """The API returns `"Results": null` rather than `[]` when nothing
    matches, which a naive client reads as a malformed body.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"Count": 0, "Results": None})

    client = _client(handler)
    rows = [row async for row in client.list_all("compute/PhysicalSummaries")]
    await client.aclose()

    assert rows == []


@pytest.mark.asyncio
async def test_a_filter_is_sent_only_when_there_is_one() -> None:
    """An empty `$filter` is not the same as no `$filter`."""
    captured: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(dict(request.url.params))
        return _page([])

    client = _client(handler)
    _ = [r async for r in client.list_all("compute/PhysicalSummaries")]
    _ = [
        r
        async for r in client.list_all(
            "compute/PhysicalSummaries", filter_expr="ManagementMode eq 'Intersight'"
        )
    ]
    await client.aclose()

    assert "$filter" not in captured[0]
    assert captured[1]["$filter"] == "ManagementMode eq 'Intersight'"


# --- throttling and retries -------------------------------------------


@pytest.mark.asyncio
async def test_a_throttled_request_is_retried_and_then_succeeds(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Cisco publishes no rate limit, so 429 is expected rather than
    exceptional and must not end the run.
    """
    slept: list[float] = []

    async def _no_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr("app.infrastructure.providers.intersight.client.asyncio.sleep", _no_sleep)

    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "7"}, json={})
        return _page([{"Moid": "a"}])

    client = _client(handler)
    rows = [row async for row in client.list_all("compute/PhysicalSummaries")]
    await client.aclose()

    assert [row["Moid"] for row in rows] == ["a"]
    assert slept == [7.0], "Retry-After must be honoured over our own backoff"


@pytest.mark.asyncio
async def test_persistent_throttling_ends_with_an_actionable_error(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Spending the whole budget is reported as throttling specifically,
    not as a generic HTTP failure.
    """

    async def _no_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr("app.infrastructure.providers.intersight.client.asyncio.sleep", _no_sleep)

    client = _client(lambda request: httpx.Response(429, json={}), max_retries=2)
    with pytest.raises(IntersightThrottledError, match="INVENTORY_INTERSIGHT_PAGE_SIZE"):
        _ = [row async for row in client.list_all("compute/PhysicalSummaries")]
    await client.aclose()


@pytest.mark.asyncio
async def test_a_bad_request_is_not_retried() -> None:
    """Repeating a request the server called malformed only wastes the
    budget — and would mask the real cause behind a throttling message.
    """
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, json={})

    client = _client(handler)
    with pytest.raises(IntersightProtocolError, match="400"):
        _ = [row async for row in client.list_all("compute/PhysicalSummaries")]
    await client.aclose()

    assert calls["n"] == 1


# --- failures an operator can fix -------------------------------------


@pytest.mark.asyncio
async def test_a_401_names_every_cause_it_cannot_distinguish() -> None:
    """Intersight answers an expired key, a revoked key, a wrong id and a
    mismatched key with the same 401, so the message has to enumerate
    them rather than guess.
    """
    client = _client(lambda request: httpx.Response(401, json={}))
    with pytest.raises(IntersightAuthError) as excinfo:
        await client.health_check()
    await client.aclose()

    message = str(excinfo.value)
    assert "INVENTORY_INTERSIGHT_USERNAME" in message
    assert "expired" in message and "revoked" in message


@pytest.mark.asyncio
async def test_a_401_with_a_skewed_clock_blames_the_clock() -> None:
    """A CronJob pod on a drifted node is otherwise indistinguishable
    from a bad credential, and sends the operator to the wrong place.
    """
    client = _client(
        lambda request: httpx.Response(
            401, headers={"Date": "Thu, 28 Aug 2025 16:53:20 GMT"}, json={}
        )
    )
    with pytest.raises(IntersightAuthError, match="clock"):
        await client.health_check()
    await client.aclose()


@pytest.mark.asyncio
async def test_a_403_points_at_the_api_key_s_role() -> None:
    """Authenticated but unauthorised is a different fix from a 401."""
    client = _client(lambda request: httpx.Response(403, json={}))
    with pytest.raises(IntersightForbiddenError, match="Read-Only"):
        await client.health_check()
    await client.aclose()


@pytest.mark.asyncio
async def test_an_unreachable_endpoint_says_so() -> None:
    """The air-gapped case: an appliance that is not there at all."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nope", request=request)

    client = _client(handler)
    with pytest.raises(IntersightUnreachableError, match="unreachable"):
        await client.health_check()
    await client.aclose()


@pytest.mark.asyncio
async def test_a_non_json_body_is_a_protocol_error() -> None:
    """A captive portal or a proxy error page, not a fleet of servers."""
    client = _client(lambda request: httpx.Response(200, text="<html>login</html>"))
    with pytest.raises(IntersightProtocolError, match="non-JSON"):
        await client.health_check()
    await client.aclose()


# --- endpoint validation ----------------------------------------------


def test_a_url_endpoint_is_rejected_with_the_host_to_use() -> None:
    """`https://https://host` is the failure this prevents."""
    with pytest.raises(ValueError, match=re.escape("use 'intersight.com'")):
        validate_endpoint("https://intersight.com")


def test_a_port_is_rejected() -> None:
    """The API is HTTPS on 443; a port here would be signed into the
    `Host` header and rejected.
    """
    with pytest.raises(ValueError, match="must not include a port"):
        validate_endpoint("appliance.example.com:8443")


def test_an_empty_endpoint_is_rejected() -> None:
    """Reached only if the credential resolver's own check is bypassed."""
    with pytest.raises(ValueError, match="empty"):
        validate_endpoint("  ")


def test_a_bare_host_is_accepted_and_lowercased() -> None:
    """Casing varies in how operators write an FQDN; the `Host` header
    the signature covers must not.
    """
    assert validate_endpoint("  Appliance.Example.COM ") == "appliance.example.com"


# --- what must never be logged ----------------------------------------


@pytest.mark.asyncio
async def test_debug_tracing_logs_no_header_body_or_key() -> None:
    """`--debug-http` exists to answer "what did it ask for", never
    "with what credential".
    """
    from structlog.testing import capture_logs

    client = _client(lambda request: _page([]), debug_http=True)
    with capture_logs() as logs:
        _ = [row async for row in client.list_all("compute/PhysicalSummaries")]
    await client.aclose()

    assert logs, "debug_http should have logged something"
    rendered = json.dumps(logs)
    assert "Signature" not in rendered
    assert "BEGIN" not in rendered
    assert "Authorization" not in rendered
    assert "/api/v1/compute/PhysicalSummaries" in rendered


# --- the API's own error document ------------------------------------
#
# Shape confirmed against the live intersight.com service on 2026-08-29:
# {"code","message","messageId","traceId"}. The research notes had marked
# this schema UNVERIFIED.


@pytest.mark.asyncio
async def test_an_error_surfaces_intersights_own_message_and_trace_id() -> None:
    """The `traceId` is the only handle Cisco can use to find this exact
    request, so discarding it costs an operator their support case.
    """
    body = {
        "code": "UnauthorizedOperation",
        "message": "Cannot process the request. The authorization header is invalid.",
        "messageId": "iam_apikey_authheader_invalid",
        "traceId": "gYSrkd4GlTKLsQjg6vDZ",
    }
    client = _client(lambda request: httpx.Response(401, json=body))
    with pytest.raises(IntersightAuthError) as excinfo:
        await client.health_check()
    await client.aclose()

    message = str(excinfo.value)
    assert "authorization header is invalid" in message
    assert "gYSrkd4GlTKLsQjg6vDZ" in message
    # Our own guidance survives alongside it.
    assert "INVENTORY_INTERSIGHT_USERNAME" in message


@pytest.mark.asyncio
async def test_an_error_without_a_body_still_produces_our_guidance() -> None:
    """Not every failure carries a document, and a missing one must not
    swallow the part of the message that says what to do.
    """
    client = _client(lambda request: httpx.Response(403, text="nope"))
    with pytest.raises(IntersightForbiddenError, match="Read-Only"):
        await client.health_check()
    await client.aclose()
