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


class Breakdown(BaseModel):
    """The counts one slice of the fleet reports.

    Shared by a whole site and by each installation-type slice within it,
    so the UI renders both with one component instead of two that can
    drift apart.
    """

    total: int = 0

    # A list, not a dict, so the UI renders vendors in a stable order
    # without sorting keys — the order is `Vendor`'s declaration order.
    by_vendor: list[VendorCount] = Field(default_factory=list)

    # Keyed by `HealthSeverity` value. Always contains every severity,
    # including zeroes, so the UI never has to distinguish "no critical
    # servers" from "the key is missing".
    by_health: dict[str, int] = Field(default_factory=dict)

    in_maintenance: int = 0


class SiteStats(Breakdown):
    site_id: str
    name: str

    # Keyed by `InstallationType` value, always containing every one.
    # The fleet-wide UPI/hosted totals are summed from these client-side,
    # exactly as the "across all sites" card sums the sites themselves —
    # a derived number can then never disagree with the cards beside it.
    by_installation_type: dict[str, Breakdown] = Field(default_factory=dict)


class SiteStatsListResponse(BaseModel):
    items: list[SiteStats]
