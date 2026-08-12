from __future__ import annotations

from pydantic import BaseModel, Field

from app.domain.models.common import AuditFields


class Site(BaseModel):
    id: str = Field(alias="_id")
    name: str
    code: str
    enabled: bool = True
    metadata: dict[str, str] = Field(default_factory=dict)
    audit: AuditFields

    model_config = {"populate_by_name": True}
