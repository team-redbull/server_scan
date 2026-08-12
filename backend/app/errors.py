"""Application error model.

Every raised `AppError` becomes an RFC 9457 ("Problem Details for HTTP
APIs") response via the handlers in `app.exception_handlers`:

    {
      "type": "/problems/revision-conflict",
      "title": "Revision Conflict",
      "status": 409,
      "detail": "The record was modified since you last read it.",
      "instance": "/api/v1/servers/srv_abc123",
      "code": "REVISION_CONFLICT",
      "request_id": "req_...",
      "details": {"current_revision": 5}
    }

RFC 9457 is the current IETF standard for HTTP API error bodies and is what
API-consuming tooling increasingly expects by default; `type`/`title`/
`status`/`detail`/`instance` are its fixed members, served with content-type
`application/problem+json`. `code`, `request_id`, and `details` are RFC 9457
"extension members" (explicitly permitted by the spec) that give this API's
own UI and any future automation a stable, machine-matchable error code on
top of the standard envelope — `code` never changes across a message
wording tweak, where `title`/`detail` might.

`type` is a relative reference (`/problems/<slug>`), not a dereferenceable
absolute URL — RFC 9457 explicitly allows this, and an air-gapped platform
has no public domain for it to resolve against anyway.

`ErrorCode` is an append-only registry: new codes are added at the end of
their section, and existing codes are never renumbered or removed once
shipped, since clients may match on them.
"""

from __future__ import annotations

from typing import Any


class ErrorCode:
    """Stable, machine-matchable error identifiers returned to API clients."""

    # --- Generic / cross-cutting ---
    VALIDATION_ERROR = "VALIDATION_ERROR"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    REVISION_CONFLICT = "REVISION_CONFLICT"
    UNAUTHORIZED = "UNAUTHORIZED"
    FORBIDDEN = "FORBIDDEN"
    RATE_LIMITED = "RATE_LIMITED"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"

    # --- Search / pagination ---
    UNKNOWN_FILTER = "UNKNOWN_FILTER"
    UNKNOWN_SORT_FIELD = "UNKNOWN_SORT_FIELD"
    SEARCH_QUERY_TOO_SHORT = "SEARCH_QUERY_TOO_SHORT"
    SEARCH_QUERY_TOO_LONG = "SEARCH_QUERY_TOO_LONG"
    PAGE_SIZE_TOO_LARGE = "PAGE_SIZE_TOO_LARGE"
    CURSOR_INVALID = "CURSOR_INVALID"
    CURSOR_FILTER_MISMATCH = "CURSOR_FILTER_MISMATCH"

    # --- Classification ---
    REGEX_UNSAFE = "REGEX_UNSAFE"
    REGEX_INVALID = "REGEX_INVALID"

    # --- Health policies ---
    METRIC_OPERATOR_MISMATCH = "METRIC_OPERATOR_MISMATCH"
    UNKNOWN_METRIC = "UNKNOWN_METRIC"
    TEMPLATE_INVALID = "TEMPLATE_INVALID"

    # --- Managers ---
    MANAGER_HAS_CHILDREN = "MANAGER_HAS_CHILDREN"
    INVALID_MANAGER_HIERARCHY = "INVALID_MANAGER_HIERARCHY"


def _slug(code: str) -> str:
    """ "REVISION_CONFLICT" -> "revision-conflict", for the `type` member."""
    return code.lower().replace("_", "-")


def _title(code: str) -> str:
    """ "REVISION_CONFLICT" -> "Revision Conflict", for the `title` member."""
    return code.replace("_", " ").title()


class AppError(Exception):
    """Base class for all application errors that should reach the client
    as an RFC 9457 problem-details body rather than an unhandled-exception
    500.
    """

    status_code: int = 500
    code: str = ErrorCode.INTERNAL_ERROR

    def __init__(self, detail: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.details = details or {}

    @property
    def type(self) -> str:
        return f"/problems/{_slug(self.code)}"

    @property
    def title(self) -> str:
        return _title(self.code)


class ValidationAppError(AppError):
    status_code = 422
    code = ErrorCode.VALIDATION_ERROR


class NotFoundError(AppError):
    status_code = 404
    code = ErrorCode.NOT_FOUND


class ConflictError(AppError):
    status_code = 409
    code = ErrorCode.CONFLICT


class RevisionConflictError(ConflictError):
    code = ErrorCode.REVISION_CONFLICT

    def __init__(self, detail: str, *, current_revision: int) -> None:
        super().__init__(detail, details={"current_revision": current_revision})


class UnauthorizedError(AppError):
    status_code = 401
    code = ErrorCode.UNAUTHORIZED


class ForbiddenError(AppError):
    status_code = 403
    code = ErrorCode.FORBIDDEN


class RateLimitedError(AppError):
    status_code = 429
    code = ErrorCode.RATE_LIMITED


class ServiceUnavailableError(AppError):
    status_code = 503
    code = ErrorCode.SERVICE_UNAVAILABLE
