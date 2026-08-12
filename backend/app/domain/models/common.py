"""Shared model building blocks.

`AuditFields` carries the optimistic-concurrency `revision` counter plus
creation/update timestamps that every top-level mutable document needs.
Composed in, not inherited from — Pydantic v2 handles composition of
`BaseModel` fields cleanly via multiple inheritance, but keeping it as an
explicit field (`audit: AuditFields`) rather than mixed-in base-class
fields makes it obvious in every document's JSON shape, and keeps each
domain model's own field list free of boilerplate to scan past.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.utils.timeutil import utcnow


class AuditFields(BaseModel):
    model_config = ConfigDict(frozen=False)

    revision: int = 1
    created_at: datetime
    updated_at: datetime

    @classmethod
    def new(cls) -> AuditFields:
        now = utcnow()
        return cls(revision=1, created_at=now, updated_at=now)
