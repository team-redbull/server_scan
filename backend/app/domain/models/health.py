"""Health rollup, embedded on `Server`.

Like `classification.py`, this declares the shape the health *engine*
(slice 3) writes to — evaluation/policy resolution logic lives there, not
here. A server with no policies evaluated yet is `UNKNOWN` in every
category, which is the correct reading of "no policy has said anything
about this yet", not a claim that the server is unhealthy.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from app.domain.enums import HealthSeverity


class CategoryHealth(BaseModel):
    severity: HealthSeverity = HealthSeverity.UNKNOWN


class Health(BaseModel):
    overall: HealthSeverity = HealthSeverity.UNKNOWN
    cpu: HealthSeverity = HealthSeverity.UNKNOWN
    memory: HealthSeverity = HealthSeverity.UNKNOWN
    storage: HealthSeverity = HealthSeverity.UNKNOWN
    network: HealthSeverity = HealthSeverity.UNKNOWN
    connectivity: HealthSeverity = HealthSeverity.UNKNOWN
    power: HealthSeverity = HealthSeverity.UNKNOWN
    evaluated_at: datetime | None = None
