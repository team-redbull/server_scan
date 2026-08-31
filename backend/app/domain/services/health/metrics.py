"""The health metric registry.

Declared in code, not the database: every metric a health policy can
reference must be a known, typed entry here, so an operator/metric
mismatch (`COUNT_GT` against a scalar, `IN` with a non-list value) is
rejected when a policy is *saved*, not discovered mid-evaluation against
10,000 servers. Extensible per module — a future vendor package registers
its own metrics by calling `register()` at import time, the same way core
metrics are registered below; `register()` raises on a duplicate name so a
naming collision between two provider packages is a loud startup failure,
not a silent shadow.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class MetricType(StrEnum):
    INT = "INT"
    FLOAT = "FLOAT"
    STRING = "STRING"
    BOOL = "BOOL"
    ENUM = "ENUM"
    LIST_STRING = "LIST_STRING"
    LIST_INT = "LIST_INT"


@dataclass(frozen=True, slots=True)
class MetricDef:
    name: str
    type: MetricType
    category: str  # cpu | memory | storage | network | connectivity | power
    description: str
    resolver: Callable[[dict[str, Any]], Any]
    enum_values: tuple[str, ...] | None = None
    provider: str = "core"


class MetricRegistry:
    """Not a global singleton — constructed once at app startup
    (`app.domain.services.health.metrics.build_default_registry()`) and
    passed explicitly to the evaluator, so tests can build a smaller
    registry without monkeypatching module state.
    """

    def __init__(self) -> None:
        self._metrics: dict[str, MetricDef] = {}

    def register(self, metric: MetricDef) -> None:
        if metric.name in self._metrics:
            raise ValueError(f"metric {metric.name!r} is already registered")
        self._metrics[metric.name] = metric

    def get(self, name: str) -> MetricDef | None:
        return self._metrics.get(name)

    def __contains__(self, name: str) -> bool:
        return name in self._metrics

    def all(self) -> list[MetricDef]:
        return sorted(self._metrics.values(), key=lambda m: m.name)


# --- Fact resolvers ---
# Each resolver takes the flat `facts` dict built by
# `app.domain.services.health.facts.extract_facts` and returns the metric's
# value. Resolvers never raise for "value not present" — they return a
# type-appropriate empty/zero value, since "no drives" and "unknown drive
# count" are different claims a resolver has no business collapsing.


def _get(facts: dict[str, Any], key: str, default: Any) -> Any:
    return facts.get(key, default)


def build_default_registry() -> MetricRegistry:
    registry = MetricRegistry()

    registry.register(
        MetricDef(
            name="cpu.socket_count",
            type=MetricType.INT,
            category="cpu",
            description="Number of populated CPU sockets",
            resolver=lambda f: _get(f, "cpu.socket_count", 0),
        )
    )
    registry.register(
        MetricDef(
            name="memory.total_bytes",
            type=MetricType.INT,
            category="memory",
            description="Total installed memory in bytes",
            resolver=lambda f: _get(f, "memory.total_bytes", 0),
        )
    )
    registry.register(
        MetricDef(
            name="storage.drive_count",
            type=MetricType.INT,
            category="storage",
            description="Number of storage drives reported",
            resolver=lambda f: _get(f, "storage.drive_count", 0),
        )
    )
    registry.register(
        MetricDef(
            name="storage.drive_healths",
            type=MetricType.LIST_STRING,
            category="storage",
            description="Health string reported per drive",
            resolver=lambda f: _get(f, "storage.drive_healths", []),
        )
    )
    registry.register(
        MetricDef(
            name="storage.failed_drive_count",
            type=MetricType.INT,
            category="storage",
            description="Count of drives with health == CRITICAL",
            resolver=lambda f: _get(f, "storage.failed_drive_count", 0),
        )
    )
    registry.register(
        MetricDef(
            name="network.interface_link_states",
            type=MetricType.LIST_STRING,
            category="network",
            description="Link state reported per network interface",
            resolver=lambda f: _get(f, "network.interface_link_states", []),
        )
    )
    registry.register(
        MetricDef(
            name="connectivity.fabric_paths_total",
            type=MetricType.INT,
            category="connectivity",
            description="Total fabric attachments reported",
            resolver=lambda f: _get(f, "connectivity.fabric_paths_total", 0),
        )
    )
    registry.register(
        MetricDef(
            name="connectivity.fabric_paths_up",
            type=MetricType.INT,
            category="connectivity",
            description="Fabric attachments with oper_state == UP",
            resolver=lambda f: _get(f, "connectivity.fabric_paths_up", 0),
        )
    )
    registry.register(
        MetricDef(
            name="connectivity.fabric_paths_down",
            type=MetricType.INT,
            category="connectivity",
            description="Fabric attachments with oper_state == DOWN",
            resolver=lambda f: _get(f, "connectivity.fabric_paths_down", 0),
        )
    )
    registry.register(
        MetricDef(
            name="power.psu_count",
            type=MetricType.INT,
            category="power",
            description="Number of power supplies reported",
            resolver=lambda f: _get(f, "power.psu_count", 0),
        )
    )
    registry.register(
        MetricDef(
            name="power.failed_psu_count",
            type=MetricType.INT,
            category="power",
            description="Count of PSUs with health != OK",
            resolver=lambda f: _get(f, "power.failed_psu_count", 0),
        )
    )
    return registry


# Operator -> the metric types it's valid to use it against. Enforced at
# policy write time (see `app.domain.services.health.conditions.
# validate_condition`), not at evaluation time.
OPERATOR_ALLOWED_TYPES: dict[str, frozenset[MetricType]] = {
    "EQ": frozenset(
        {MetricType.INT, MetricType.FLOAT, MetricType.STRING, MetricType.BOOL, MetricType.ENUM}
    ),
    "NE": frozenset(
        {MetricType.INT, MetricType.FLOAT, MetricType.STRING, MetricType.BOOL, MetricType.ENUM}
    ),
    "GT": frozenset({MetricType.INT, MetricType.FLOAT}),
    "GTE": frozenset({MetricType.INT, MetricType.FLOAT}),
    "LT": frozenset({MetricType.INT, MetricType.FLOAT}),
    "LTE": frozenset({MetricType.INT, MetricType.FLOAT}),
    "IN": frozenset({MetricType.INT, MetricType.FLOAT, MetricType.STRING, MetricType.ENUM}),
    "NOT_IN": frozenset({MetricType.INT, MetricType.FLOAT, MetricType.STRING, MetricType.ENUM}),
    "EXISTS": frozenset(MetricType),
    "NOT_EXISTS": frozenset(MetricType),
    "ANY": frozenset({MetricType.LIST_STRING, MetricType.LIST_INT}),
    "ALL": frozenset({MetricType.LIST_STRING, MetricType.LIST_INT}),
    "COUNT_EQ": frozenset({MetricType.LIST_STRING, MetricType.LIST_INT}),
    "COUNT_GT": frozenset({MetricType.LIST_STRING, MetricType.LIST_INT}),
    "COUNT_GTE": frozenset({MetricType.LIST_STRING, MetricType.LIST_INT}),
    "COUNT_LT": frozenset({MetricType.LIST_STRING, MetricType.LIST_INT}),
    "COUNT_LTE": frozenset({MetricType.LIST_STRING, MetricType.LIST_INT}),
}
