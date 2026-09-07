"""Adapters for everything outside the domain.

MongoDB, Redis, vendor collectors, credential resolution, structured
logging, and the `singleflight`/`blocking` cross-cutting concurrency
helpers.

This file's own existence matters beyond its docstring: before it was
added, `app.infrastructure` was an implicit namespace package, which
silently defeated ruff's isort first-party detection for the whole
`app.*` tree (see `docs/notes/2026-09-refactor-plan.md`, Phase 8).
"""
