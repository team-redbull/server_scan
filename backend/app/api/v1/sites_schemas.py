"""Response models for `GET /api/v1/sites`.

Mutable (not frozen) on purpose: `app.api.v1.sites._pivot` builds these
incrementally as it folds the aggregation buckets, which is the one place
they are constructed.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class VendorCount(BaseModel):
    vendor: str
    count: int


class SiteStats(BaseModel):
    site_id: str
    name: str
    total: int

    # A list, not a dict, so the UI renders vendors in a stable order
    # without sorting keys — the order is `Vendor`'s declaration order.
    by_vendor: list[VendorCount] = Field(default_factory=list)

    # Keyed by `HealthSeverity` value. Always contains every severity,
    # including zeroes, so the UI never has to distinguish "no critical
    # servers" from "the key is missing".
    by_health: dict[str, int] = Field(default_factory=dict)

    in_maintenance: int = 0


class SiteStatsListResponse(BaseModel):
    items: list[SiteStats]
