"""Health policy condition tree: model, write-time validation, evaluation.

A condition is either a composite node (`all_of` / `any_of` / `not`) or a
leaf (`metric` + `operator` [+ `value`]). No `eval`, no `exec`, no stored
code of any kind — this is a closed, declarative grammar, validated
against the metric registry before it's ever saved.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator

from app.domain.services.health.metrics import OPERATOR_ALLOWED_TYPES, MetricRegistry, MetricType

MAX_CONDITION_DEPTH = 5
MAX_CONDITION_NODES = 50

_SCALAR_OPERATORS = frozenset({"EQ", "NE", "GT", "GTE", "LT", "LTE"})
_SET_OPERATORS = frozenset({"IN", "NOT_IN"})
_EXISTENCE_OPERATORS = frozenset({"EXISTS", "NOT_EXISTS"})
_LIST_ELEMENT_OPERATORS = frozenset({"ANY", "ALL"})
_COUNT_OPERATORS = frozenset({"COUNT_EQ", "COUNT_GT", "COUNT_GTE", "COUNT_LT", "COUNT_LTE"})
ALL_OPERATORS = (
    _SCALAR_OPERATORS
    | _SET_OPERATORS
    | _EXISTENCE_OPERATORS
    | _LIST_ELEMENT_OPERATORS
    | _COUNT_OPERATORS
)


class ConditionValidationError(Exception):
    pass


class Condition(BaseModel):
    """A single node: exactly one of (`all_of`, `any_of`, `not_`, `metric`)
    must be set. Pydantic's `model_validator` enforces that shape rather
    than a discriminated union, since the "one of N mutually exclusive
    field groups" shape doesn't map cleanly onto a `Literal`-tagged union
    without an artificial `kind` field no caller would ever want to type.
    """

    all_of: list[Condition] | None = None
    any_of: list[Condition] | None = None
    not_: Condition | None = Field(default=None, alias="not")

    metric: str | None = None
    operator: str | None = None
    value: Any = None
    # COUNT_* only: the element value to count matches for. If unset, a
    # COUNT_* operator counts the list's own length (every element).
    equals: Any = None

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def _exactly_one_kind(self) -> Condition:
        kinds = [
            self.all_of is not None,
            self.any_of is not None,
            self.not_ is not None,
            self.metric is not None,
        ]
        if sum(kinds) != 1:
            raise ValueError("exactly one of all_of/any_of/not/metric must be set")
        if self.metric is not None and self.operator is None:
            raise ValueError("a metric leaf requires an operator")
        if self.all_of is not None and len(self.all_of) == 0:
            raise ValueError("all_of must be non-empty")
        if self.any_of is not None and len(self.any_of) == 0:
            raise ValueError("any_of must be non-empty")
        return self

    def is_leaf(self) -> bool:
        return self.metric is not None


def _depth(condition: Condition) -> int:
    if condition.all_of is not None:
        return 1 + max((_depth(c) for c in condition.all_of), default=0)
    if condition.any_of is not None:
        return 1 + max((_depth(c) for c in condition.any_of), default=0)
    if condition.not_ is not None:
        return 1 + _depth(condition.not_)
    return 1


def _node_count(condition: Condition) -> int:
    if condition.all_of is not None:
        return 1 + sum(_node_count(c) for c in condition.all_of)
    if condition.any_of is not None:
        return 1 + sum(_node_count(c) for c in condition.any_of)
    if condition.not_ is not None:
        return 1 + _node_count(condition.not_)
    return 1


def validate_condition(condition: Condition, registry: MetricRegistry) -> None:
    """Raises `ConditionValidationError` for anything that would be unsafe
    or meaningless to evaluate: unknown metric, operator/type mismatch, or
    a tree that's too deep/large. Called when a policy is created/updated,
    never at evaluation time — evaluation trusts a condition that passed
    this once.
    """
    if _depth(condition) > MAX_CONDITION_DEPTH:
        raise ConditionValidationError(f"condition tree exceeds max depth {MAX_CONDITION_DEPTH}")
    if _node_count(condition) > MAX_CONDITION_NODES:
        raise ConditionValidationError(
            f"condition tree exceeds max node count {MAX_CONDITION_NODES}"
        )

    if condition.all_of is not None:
        for child in condition.all_of:
            validate_condition(child, registry)
        return
    if condition.any_of is not None:
        for child in condition.any_of:
            validate_condition(child, registry)
        return
    if condition.not_ is not None:
        validate_condition(condition.not_, registry)
        return

    if condition.metric is None or condition.operator is None:
        raise AssertionError(
            "leaf condition missing metric/operator"
        )  # enforced by the model validator
    metric = registry.get(condition.metric)
    if metric is None:
        raise ConditionValidationError(f"unknown metric {condition.metric!r}")
    if condition.operator not in ALL_OPERATORS:
        raise ConditionValidationError(f"unknown operator {condition.operator!r}")
    allowed_types = OPERATOR_ALLOWED_TYPES[condition.operator]
    if metric.type not in allowed_types:
        raise ConditionValidationError(
            f"operator {condition.operator!r} is not valid for metric type {metric.type.value}"
        )
    if metric.type is MetricType.ENUM and condition.operator in (
        _SCALAR_OPERATORS | _SET_OPERATORS
    ):
        values = condition.value if isinstance(condition.value, list) else [condition.value]
        for v in values:
            if metric.enum_values is not None and v not in metric.enum_values:
                raise ConditionValidationError(
                    f"value {v!r} is not a valid value for {metric.name!r}"
                )


def _eval_scalar(operator: str, actual: Any, value: Any) -> bool:
    if operator == "EQ":
        return bool(actual == value)
    if operator == "NE":
        return bool(actual != value)
    if operator == "GT":
        return bool(actual > value)
    if operator == "GTE":
        return bool(actual >= value)
    if operator == "LT":
        return bool(actual < value)
    if operator == "LTE":
        return bool(actual <= value)
    raise AssertionError(f"not a scalar operator: {operator}")


def evaluate_leaf(condition: Condition, facts: dict[str, Any], registry: MetricRegistry) -> bool:
    if condition.metric is None or condition.operator is None:
        raise AssertionError(
            "leaf condition missing metric/operator"
        )  # enforced by the model validator
    metric = registry.get(condition.metric)
    if metric is None:
        raise ConditionValidationError(f"unknown metric {condition.metric!r}")
    actual = metric.resolver(facts)
    op = condition.operator

    if op in _EXISTENCE_OPERATORS:
        exists = actual is not None and actual != [] and actual != ""
        return exists if op == "EXISTS" else not exists
    if op in _SCALAR_OPERATORS:
        return _eval_scalar(op, actual, condition.value)
    if op == "IN":
        return bool(actual in condition.value)
    if op == "NOT_IN":
        return bool(actual not in condition.value)
    if op == "ANY":
        return bool(condition.value in actual)
    if op == "ALL":
        # Vacuous truth on an empty list is deliberately NOT returned here:
        # "all NICs are UP" evaluating true when zero NICs were even
        # reported would hide a data-collection gap, not confirm a healthy
        # server.
        return len(actual) > 0 and all(x == condition.value for x in actual)
    if op in _COUNT_OPERATORS:
        count = (
            len(actual)
            if condition.equals is None
            else sum(1 for x in actual if x == condition.equals)
        )
        threshold = condition.value
        scalar_op = op.removeprefix("COUNT_")
        return _eval_scalar(scalar_op, count, threshold)
    raise AssertionError(f"unhandled operator: {op}")


def evaluate_condition(
    condition: Condition, facts: dict[str, Any], registry: MetricRegistry
) -> bool:
    if condition.all_of is not None:
        return all(evaluate_condition(c, facts, registry) for c in condition.all_of)
    if condition.any_of is not None:
        return any(evaluate_condition(c, facts, registry) for c in condition.any_of)
    if condition.not_ is not None:
        return not evaluate_condition(condition.not_, facts, registry)
    return evaluate_leaf(condition, facts, registry)


def leaf_metrics(condition: Condition) -> list[str]:
    """All metric names referenced anywhere in the tree, for evidence
    collection (`app.domain.services.health.evaluate`).
    """
    if condition.all_of is not None:
        return [m for c in condition.all_of for m in leaf_metrics(c)]
    if condition.any_of is not None:
        return [m for c in condition.any_of for m in leaf_metrics(c)]
    if condition.not_ is not None:
        return leaf_metrics(condition.not_)
    if condition.metric is None:
        raise AssertionError("leaf condition missing metric")  # enforced by the model validator
    return [condition.metric]
