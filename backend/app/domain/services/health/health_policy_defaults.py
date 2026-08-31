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

    return [fabric_warning, fabric_critical, failed_drive]
