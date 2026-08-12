# ADR-0002: RFC 9457 Problem Details for API errors

## Status

Accepted

## Context

Every API needs one consistent error shape so clients (the UI now, other
automation later) never need endpoint-specific parsing. The two live
options are a bespoke JSON envelope, or the IETF's standardized format.

## Decision

Use [RFC 9457](https://www.rfc-editor.org/rfc/rfc9457) "Problem Details for
HTTP APIs", served as `application/problem+json`:

```json
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
```

`type`/`title`/`status`/`detail`/`instance` are RFC 9457's fixed members.
`code`, `request_id`, and `details` are RFC 9457 "extension members" —
explicitly permitted by the spec — giving this platform's own clients a
stable, machine-matchable code that doesn't shift if `detail`'s wording is
edited. `type` is a relative reference (`/problems/<slug>`) rather than a
dereferenceable absolute URL, which the RFC allows and which fits an
air-gapped platform with no public domain for it to resolve against.

Implemented by hand in `app.errors` / `app.exception_handlers` rather than
via the one community package for this (`fastapi-problem`) — the format is
a handful of fields, not worth an extra dependency in an air-gapped
mirror.

## Consequences

- Every `AppError` subclass and the built-in FastAPI/Starlette exception
  types funnel through one renderer (`app.exception_handlers`); no route
  handler builds its own error body.
- `ErrorCode` is an append-only registry (`app/errors.py`) — codes are
  never renumbered or removed once shipped, since clients may match on
  them.
- Frontend error handling has one shape to parse everywhere, including for
  errors FastAPI/Starlette raise internally (404 route-not-found, 422
  validation) — those are also normalized into the same envelope.
