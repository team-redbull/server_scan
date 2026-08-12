"""Core domain enumerations.

Plain `str, Enum` (not a bare string-constant class like `ErrorCode`)
because these values are embedded directly in Pydantic domain models and
benefit from Pydantic's enum validation/serialization — invalid values are
rejected at the model boundary rather than accepted as arbitrary strings.
`ErrorCode` stays a string-constant class because it is never a model
field, only ever compared against; an enum would add nothing there.
"""

from __future__ import annotations

from enum import StrEnum


class Vendor(StrEnum):
    DELL = "dell"
    CISCO = "cisco"
    HPE = "hpe"
    UNKNOWN = "unknown"


class ManagerType(StrEnum):
    OPENMANAGE = "OPENMANAGE"
    UCS_MANAGER = "UCS_MANAGER"
    UCS_CENTRAL = "UCS_CENTRAL"
    INTERSIGHT = "INTERSIGHT"
    ONEVIEW = "ONEVIEW"


class InstallationType(StrEnum):
    HOSTED_CLUSTER = "HOSTED_CLUSTER"
    UPI = "UPI"
    UNCLASSIFIED = "UNCLASSIFIED"


class HealthSeverity(StrEnum):
    """Ordering matters and is defined once here (`RANK`) — every
    aggregation in the health engine sorts by this, never by enum
    declaration order or alphabetical order.
    """

    UNKNOWN = "UNKNOWN"
    HEALTHY = "HEALTHY"
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


HEALTH_SEVERITY_RANK: dict[HealthSeverity, int] = {
    HealthSeverity.UNKNOWN: 0,
    HealthSeverity.HEALTHY: 1,
    HealthSeverity.INFO: 2,
    HealthSeverity.WARNING: 3,
    HealthSeverity.CRITICAL: 4,
}


class LinkState(StrEnum):
    UP = "UP"
    DOWN = "DOWN"
    UNKNOWN = "UNKNOWN"
    DISABLED = "DISABLED"


class MediaType(StrEnum):
    HDD = "HDD"
    SSD = "SSD"
    NVME = "NVME"
    UNKNOWN = "UNKNOWN"
