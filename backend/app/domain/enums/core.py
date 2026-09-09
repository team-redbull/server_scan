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
    """The three vendors this platform ingests from, and nothing else.

    There is deliberately no `UNKNOWN` member: every server reaches the
    platform through a vendor-specific collector
    (`app.infrastructure.providers.<vendor>`), so the vendor is known by
    construction — it is a property of *which collector produced the
    record*, never something guessed from the payload. A provider that
    cannot state its vendor is a bug in that provider, and
    `Vendor("...")` raising is the correct, loud failure.

    `HP`, not `HPE`: the platform reports the vendor the way operators
    here refer to it.

    `STANDALONE` means **a manufacturer this platform does not model** —
    Lenovo, Supermicro, a whitebox — not "collected without a manager".
    That distinction matters: a Dell reached over Redfish with no
    aggregator is still `DELL`, because `IngestService` correlates on
    `(vendor, serial_normalized)` and moving a machine between vendors
    splits it into two documents. Which collector found a server is
    carried by `Server.source_provider`. See
    docs/adr/0016-redfish-standalone-collector.md.

    It is not the `UNKNOWN` this docstring argues against: it is never
    guessed from a payload. A provider that cannot read `Manufacturer` at
    all reports a collection failure rather than defaulting here.
    """

    DELL = "dell"
    CISCO = "cisco"
    HP = "hp"
    STANDALONE = "standalone"


class ManagerType(StrEnum):
    """How this platform reaches a server.

    `REDFISH_STANDALONE` is the odd one out and deliberately so: it names
    no manager at all. It is the collector for machines no aggregator
    owns, reached one BMC at a time over DMTF Redfish. See
    docs/adr/0016-redfish-standalone-collector.md.
    """

    OPENMANAGE = "OPENMANAGE"
    UCS_MANAGER = "UCS_MANAGER"
    UCS_CENTRAL = "UCS_CENTRAL"
    INTERSIGHT = "INTERSIGHT"
    ONEVIEW = "ONEVIEW"
    REDFISH_STANDALONE = "REDFISH_STANDALONE"


class InstallationType(StrEnum):
    """A server's role, as a regex verdict on its hostname (classification)."""

    HOSTED_CLUSTER = "HOSTED_CLUSTER"
    MCE = "MCE"
    UPI = "UPI"
    UNCLASSIFIED = "UNCLASSIFIED"


class OpenShiftState(StrEnum):
    """
    Whether a server is in use, as OpenShift reports it.

    Deliberately parallel to `InstallationType` and deliberately not the
    same thing. `InstallationType` is a regex verdict on a hostname, which
    is a naming convention; this is a cluster or an MCE reporting what it
    actually holds. When they disagree the server is misnamed or
    misplaced, and that is worth seeing rather than reconciling away — see
    `app.domain.models.openshift`.

    Three states, not four: this answers "is it in use", and *what kind*
    of node it is comes from `InstallationType`. An earlier shape encoded
    the kind here too (`UPI_NODE`/`HOSTED_NODE`), which made the same fact
    answerable two ways and left no single value the inventory could
    filter on.
    """

    AVAILABLE = "AVAILABLE"
    """No cluster claims this server — free to take.

    The **default**, and the only state reached by absence rather than by
    observation. A server is available the moment it is ingested, and
    returns here when the cluster that claimed it stops listing it. There
    is deliberately no "nobody looked yet" state: it would be
    indistinguishable from this one in every place it is displayed, and
    every server has to answer the in-use question somehow."""

    INSTALLED = "INSTALLED"
    """In use: a node in a cluster, or an Agent bound to a hosted cluster."""

    INSTALLED_TO_INVENTORY = "INSTALLED_TO_INVENTORY"
    """Registered to an MCE and bound to no cluster — spare capacity that
    cluster creation can draw on. Distinct from `AVAILABLE`: an MCE holds
    this server and can deploy it without anyone racking anything."""


class HealthSeverity(StrEnum):
    """
    A server's overall or per-category health, from best to worst.

    Ordering matters and is defined once here (`HEALTH_SEVERITY_RANK`) —
    every aggregation in the health engine sorts by this, never by enum
    declaration order or alphabetical order.
    """

    UNKNOWN = "UNKNOWN"
    HEALTHY = "HEALTHY"
    INFO = "INFO"
    WARNING = "WARNING"
    # Between WARNING and CRITICAL: redundancy is gone but the server is
    # still serving. A single remaining network link and one bad OS disk
    # are the cases it exists for — both mean the next failure takes the
    # machine down, which is worth waking someone for in a way a degraded
    # data disk is not, and is not the same as being down already.
    MAJOR = "MAJOR"
    CRITICAL = "CRITICAL"


# Declaration order above is already low-to-high, but the ranks stay
# explicit: `HealthSeverity` is a `StrEnum`, so a future alphabetical
# reorder would silently reverse CRITICAL and MAJOR everywhere that
# aggregates a worst-of.
HEALTH_SEVERITY_RANK: dict[HealthSeverity, int] = {
    HealthSeverity.UNKNOWN: 0,
    HealthSeverity.HEALTHY: 1,
    HealthSeverity.INFO: 2,
    HealthSeverity.WARNING: 3,
    HealthSeverity.MAJOR: 4,
    HealthSeverity.CRITICAL: 5,
}


class LinkState(StrEnum):
    """A network interface's reported link state."""

    UP = "UP"
    DOWN = "DOWN"
    UNKNOWN = "UNKNOWN"
    DISABLED = "DISABLED"


class MediaType(StrEnum):
    """A storage drive's reported media type."""

    HDD = "HDD"
    SSD = "SSD"
    NVME = "NVME"
    UNKNOWN = "UNKNOWN"
