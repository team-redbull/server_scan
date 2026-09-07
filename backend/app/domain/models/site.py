"""The `Site` document: one row per entry in `INVENTORY_SITES`.

Projected from `app.domain.value_objects.site.SiteCatalog` to Mongo so the
API can list sites without re-parsing the catalog string on every request.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.domain.models.common import AuditFields


class Site(BaseModel):
    """One site from the catalog, as stored."""

    id: str = Field(alias="_id")
    name: str
    code: str
    enabled: bool = True
    metadata: dict[str, str] = Field(default_factory=dict)
    audit: AuditFields

    model_config = {"populate_by_name": True}
