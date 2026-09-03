"""Seeded system-default health policies.

`default_system_policies()` returns the platform spec's Cisco UCS fabric
acceptance scenario (§71) plus one generic storage default, ready to be
upserted by whatever integration step owns app-startup seeding (not this
module's job — see the module-level callers' docstrings for why this file
only *builds* the policies rather than persisting them).

The two fabric policies use DIFFERENT `policy_key`s
(`connectivity.fabric_paths_down_warning` /
`connectivity.fabric_paths_down_critical`) with mutually exclusive
conditions (`EQ 1` and `GTE 2`), matching the already-tested pattern in
`tests/unit/domain/services/test_health_evaluate.py`'s `FABRIC_POLICIES`
fixture: same `policy_key` means "these compete for one winner", and the
two severities here are meant to coexist as independent, simultaneously
registerable candidates, never to shadow each other.
"""

from __future__ import annotations

from app.domain.enums import HealthSeverity
from app.domain.models.health_policy import EvidenceField, HealthPolicy, PolicyScope
from app.domain.services.health.conditions import Condition
from app.utils.ids import new_id
from app.utils.timeutil import utcnow


def default_system_policies() -> list[HealthPolicy]:
    now = utcnow()

    # `id=` (the field name), not `_id=` (its Mongo alias): both are valid
    # at runtime under `HealthPolicy`'s `populate_by_name=True`, but
    # mypy's pydantic plugin only recognizes the field name as satisfying
    # the synthesized `__init__`'s required-argument check for this
    # model — passing the alias directly here (as `app.infrastructure.
    # mongodb`'s document-shaped code does when round-tripping `by_alias`
    # dicts) makes it report a spurious "Missing named argument 'id'".
    fabric_warning = HealthPolicy(
        id=new_id("health_policy"),
        name="UCS fabric path down (warning)",
        description="Fires when exactly one UCS fabric path is reported down.",
        policy_key="connectivity.fabric_paths_down_warning",
        category="connectivity",
        severity=HealthSeverity.WARNING,
        condition=Condition(metric="connectivity.fabric_paths_down", operator="EQ", value=1),
        evidence=[EvidenceField(key="down", metric="connectivity.fabric_paths_down")],
        message_template="{down} UCS fabric path is down",
        scope=PolicyScope(),
        source="SYSTEM_DEFAULT",
        priority=100,
        system=True,
        created_at=now,
        updated_at=now,
    )

    fabric_critical = HealthPolicy(
        id=new_id("health_policy"),
        name="UCS fabric paths down (critical)",
        description="Fires when two or more UCS fabric paths are reported down.",
        policy_key="connectivity.fabric_paths_down_critical",
        category="connectivity",
        severity=HealthSeverity.CRITICAL,
        condition=Condition(metric="connectivity.fabric_paths_down", operator="GTE", value=2),
        evidence=[EvidenceField(key="down", metric="connectivity.fabric_paths_down")],
        message_template="{down} UCS fabric paths are down",
        scope=PolicyScope(),
        source="SYSTEM_DEFAULT",
        priority=100,
        system=True,
        created_at=now,
        updated_at=now,
    )

    failed_drive = HealthPolicy(
        id=new_id("health_policy"),
        name="Failed drive present",
        description="Fires when one or more storage drives report a CRITICAL health state.",
        policy_key="storage.failed_drive",
        category="storage",
        severity=HealthSeverity.CRITICAL,
        condition=Condition(metric="storage.failed_drive_count", operator="GTE", value=1),
        evidence=[EvidenceField(key="count", metric="storage.failed_drive_count")],
        message_template="{count} drive(s) failed",
        scope=PolicyScope(),
        source="SYSTEM_DEFAULT",
        priority=100,
        system=True,
        created_at=now,
        updated_at=now,
    )

    # Everything below is vendor-neutral by construction: each condition
    # reads a fact derived from the normalized `Server` document, never a
    # vendor payload, so one policy covers Dell, Cisco, DGX and standalone
    # alike. The two fabric policies above are the exception and stay one —
    # `connectivity.fabric_paths_*` is zero for anything without a fabric
    # interconnect, so they simply never fire there.
    failed_psu = HealthPolicy(
        id=new_id("health_policy"),
        name="Power supply failed",
        description=(
            "Fires when one or more power supplies report DOWN. Covers every "
            "server kind: UCS and Intersight report PSUs from OperState, and "
            "Redfish from the chassis power subsystem."
        ),
        policy_key="power.failed_psu",
        category="power",
        severity=HealthSeverity.CRITICAL,
        condition=Condition(metric="power.failed_psu_count", operator="GTE", value=1),
        evidence=[
            EvidenceField(key="failed", metric="power.failed_psu_count"),
            EvidenceField(key="total", metric="power.psu_count"),
        ],
        message_template="{failed} of {total} power supplies failed",
        scope=PolicyScope(),
        source="SYSTEM_DEFAULT",
        priority=100,
        system=True,
        created_at=now,
        updated_at=now,
    )

    warning_drive = HealthPolicy(
        id=new_id("health_policy"),
        name="Drive degraded",
        description=(
            "Fires when a drive reports WARNING — degraded or predictive "
            "failure, but not yet failed. Distinct from storage.failed_drive, "
            "which stays CRITICAL and fires only on a dead drive."
        ),
        policy_key="storage.warning_drive",
        category="storage",
        severity=HealthSeverity.WARNING,
        condition=Condition(metric="storage.warning_drive_count", operator="GTE", value=1),
        evidence=[EvidenceField(key="count", metric="storage.warning_drive_count")],
        message_template="{count} drive(s) degraded",
        scope=PolicyScope(),
        source="SYSTEM_DEFAULT",
        priority=100,
        system=True,
        created_at=now,
        updated_at=now,
    )

    # Deliberately NOT "any link is down": a server with unused NICs has
    # down links and is perfectly healthy, so that would fire on most of
    # the fleet forever. `interface_count GTE 1` is what separates "every
    # link is down" from "no interfaces were reported at all" — the latter
    # is a collection gap, not a network failure, and must not alert.
    all_links_down = HealthPolicy(
        id=new_id("health_policy"),
        name="No network link up",
        description=(
            "Fires when a server reports interfaces and none of them is up. "
            "Says nothing about a server whose interfaces were never read."
        ),
        policy_key="network.all_links_down",
        category="network",
        severity=HealthSeverity.CRITICAL,
        condition=Condition(
            all_of=[
                Condition(metric="network.interface_count", operator="GTE", value=1),
                Condition(metric="network.links_up_count", operator="EQ", value=0),
            ]
        ),
        evidence=[EvidenceField(key="interfaces", metric="network.interface_count")],
        message_template="no network link is up across {interfaces} interface(s)",
        scope=PolicyScope(),
        source="SYSTEM_DEFAULT",
        priority=100,
        system=True,
        created_at=now,
        updated_at=now,
    )

    failed_gpu = HealthPolicy(
        id=new_id("health_policy"),
        name="GPU failed",
        description=(
            "Fires when one or more GPUs report a failed state, in either "
            "vocabulary the providers use — CRITICAL from Redfish, DOWN from "
            "UCS and Intersight."
        ),
        policy_key="gpu.failed",
        category="gpu",
        severity=HealthSeverity.CRITICAL,
        condition=Condition(metric="gpu.failed_count", operator="GTE", value=1),
        evidence=[
            EvidenceField(key="failed", metric="gpu.failed_count"),
            EvidenceField(key="total", metric="gpu.count"),
        ],
        message_template="{failed} of {total} GPUs failed",
        scope=PolicyScope(),
        source="SYSTEM_DEFAULT",
        priority=100,
        system=True,
        created_at=now,
        updated_at=now,
    )

    # WARNING at the first one, not a threshold: an uncorrectable ECC error
    # is memory the card could not repair, and on accelerator hardware it
    # is a documented predictor of a failing card rather than routine
    # noise. Correctable errors are deliberately not checked at all — those
    # are the mechanism working, and would fire constantly.
    gpu_ecc = HealthPolicy(
        id=new_id("health_policy"),
        name="GPU uncorrectable ECC errors",
        description=(
            "Fires on any uncorrectable ECC error across the server's GPUs. "
            "Only Redfish reports these; a provider that does not leaves the "
            "count at zero and this never fires."
        ),
        policy_key="gpu.uncorrectable_errors",
        category="gpu",
        severity=HealthSeverity.WARNING,
        condition=Condition(metric="gpu.uncorrectable_error_count", operator="GTE", value=1),
        evidence=[EvidenceField(key="errors", metric="gpu.uncorrectable_error_count")],
        message_template="{errors} uncorrectable GPU ECC error(s)",
        scope=PolicyScope(),
        source="SYSTEM_DEFAULT",
        priority=100,
        system=True,
        created_at=now,
        updated_at=now,
    )

    return [
        fabric_warning,
        fabric_critical,
        failed_drive,
        failed_psu,
        warning_drive,
        all_links_down,
        failed_gpu,
        gpu_ecc,
    ]
