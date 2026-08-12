"""Request-id + timing middleware.

Every request gets a request id (reused from an inbound `X-Request-Id` if the
caller supplied one — useful when a reverse proxy or the frontend generates
it — otherwise a fresh uuid4). It is bound into structlog's contextvars for
the lifetime of the request, so every log line emitted while handling it
carries `request_id` without threading it through every function call, and
it is echoed back in the response header and in every error envelope
(`app.exception_handlers`) so a user-reported error can be found in logs.

One structured "request completed" log line per request replaces uvicorn's
plain-text access log (silenced in `app.infrastructure.logging.config`).
"""

from __future__ import annotations

import time
import uuid

import structlog
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = structlog.get_logger(__name__)

_REQUEST_ID_HEADER = "X-Request-Id"


class RequestContextMiddleware:
    """Pure-ASGI middleware (not BaseHTTPMiddleware) to avoid its known
    interaction problems with streaming responses and background tasks.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        request_id = request.headers.get(_REQUEST_ID_HEADER) or f"req_{uuid.uuid4().hex}"
        request.state.request_id = request_id

        start = time.monotonic()
        status_code = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                headers = list(message.get("headers", []))
                headers.append((_REQUEST_ID_HEADER.lower().encode(), request_id.encode()))
                message = {**message, "headers": headers}
            await send(message)

        with structlog.contextvars.bound_contextvars(request_id=request_id):
            await self.app(scope, receive, send_wrapper)
            duration_ms = round((time.monotonic() - start) * 1000, 2)
            log = logger.info if status_code < 500 else logger.error
            log(
                "request.completed",
                http_method=request.method,
                http_path=request.url.path,
                http_status=status_code,
                duration_ms=duration_ms,
            )
