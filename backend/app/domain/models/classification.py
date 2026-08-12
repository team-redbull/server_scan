"""Classification result, embedded on `Server`.

The classification *engine* (rule resolution, regex evaluation) is a
slice-2 concern; this module only declares the shape a result takes once
computed, so the `Server` document schema doesn't need a migration when
the engine lands — an unclassified server today has the same document
shape a classified one will have tomorrow, just with null rule fields.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from app.domain.enums import InstallationType


class Classification(BaseModel):
    installation_type: InstallationType = InstallationType.UNCLASSIFIED
    matched_rule_id: str | None = None
    matched_pattern: str | None = None
    matched_field: str | None = None
    classified_at: datetime | None = None
    classification_version: int = 0
