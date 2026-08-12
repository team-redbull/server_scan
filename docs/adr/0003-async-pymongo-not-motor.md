# ADR-0003: PyMongo's native async driver, not Motor

## Status

Accepted

## Context

FastAPI needs a non-blocking MongoDB driver. Historically that meant
Motor. As of this project's start, PyMongo ships its own native asyncio
support (`AsyncMongoClient`), unifying PyMongo and Motor into one driver;
Motor itself entered its deprecation window (critical-fixes-only until May
2027) in May 2026.

## Decision

Use `pymongo.AsyncMongoClient` directly. One client per process, created
in FastAPI's `lifespan` with explicit `connectTimeoutMS`,
`serverSelectionTimeoutMS`, `socketTimeoutMS`, and pool size settings —
never a client constructed per request, and never defaults left implicit.

## Consequences

- No Motor dependency, ever — starting on it now would mean migrating off
  a driver already in its deprecation window before this platform reaches
  production.
- PyMongo's async client uses native `asyncio` tasks rather than Motor's
  thread-pool-executor model; this is usually a performance win but is a
  newer code path than Motor's, worth watching under load (flagged as a
  risk for the slice-6 50k-server performance pass).
- Explicit timeouts mean a hung MongoDB connection fails a request loudly
  and quickly instead of hanging it indefinitely — important in an
  air-gapped estate with no secondary region to fail over to.
