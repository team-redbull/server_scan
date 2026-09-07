"""Response models for `GET /api/v1/sites`.

Mutable (not frozen) on purpose: `app.api.v1.sites._pivot` builds these
incrementally as it folds the aggregation buckets, which is the one place
they are constructed.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class VendorCount(BaseModel):
    """How many servers of one vendor a breakdown counts."""

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
    """One site's fleet-wide breakdown, sliced further by installation type."""

    site_id: str
    name: str

    # Keyed by `InstallationType` value, always containing every one.
    by_installation_type: dict[str, Breakdown] = Field(default_factory=dict)


class FleetSummary(Breakdown):
    """Every site summed together, sliced further by installation type.

    Folded from the same `site_breakdown()` aggregation rows `SiteStats`
    is built from (`app.api.v1.sites._pivot`), in the same backend pass —
    not summed from the per-site `items` client-side, so a second
    consumer of this endpoint gets the identical fleet-wide number
    without reimplementing the sum.
    """

    by_installation_type: dict[str, Breakdown] = Field(default_factory=dict)


class SiteStatsListResponse(BaseModel):
    """Every configured site's statistics, plus "Unassigned", plus the fleet-wide summary."""

    items: list[SiteStats]
    fleet: FleetSummary
