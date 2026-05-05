"""Shared helpers for rule evaluators: dataclass, duration parser, condition evaluator."""

from dataclasses import dataclass
from typing import Any


@dataclass
class RuleEvaluation:
    """Result of evaluating a single rule against a dataset partition."""

    rule_id: str
    assertion_result: str  # "SUCCESS" | "FAILURE" | "ERROR"
    values: dict[str, Any]
    validation: dict[str, bool] | None
    issues: list[dict[str, Any]]
    partition: dict[str, Any]


def parse_duration_seconds(value: str) -> float:
    """Parse a human-readable duration string into seconds.

    Supported formats: "6h", "6 hours", "30m", "30 minutes", "1 hour",
    "2d", "2 days", "1 day", "90s", "90 seconds".
    Raises ValueError for unrecognised formats.
    """
    import re

    value = value.strip().lower()
    # Pattern: optional space between number and unit
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([a-z]+)", value)
    if not m:
        raise ValueError(f"Cannot parse duration: {value!r}")
    amount = float(m.group(1))
    unit = m.group(2)
    _unit_seconds: dict[str, float] = {
        "s": 1.0,
        "sec": 1.0,
        "second": 1.0,
        "seconds": 1.0,
        "m": 60.0,
        "min": 60.0,
        "minute": 60.0,
        "minutes": 60.0,
        "h": 3600.0,
        "hr": 3600.0,
        "hour": 3600.0,
        "hours": 3600.0,
        "d": 86400.0,
        "day": 86400.0,
        "days": 86400.0,
    }
    if unit not in _unit_seconds:
        raise ValueError(f"Unknown duration unit: {unit!r}")
    return amount * _unit_seconds[unit]


def evaluate_condition(actual_value: Any, condition: dict[str, Any]) -> tuple[bool, str]:
    """Evaluate a condition dict against an actual numeric value.

    Operator type strings mirror the Open Assertions YAML spec (OAS):
    - between: {"type": "between", "min": X, "max": Y}
    - less_than: {"type": "less_than", "value": X}
    - less_than_or_equal_to: {"type": "less_than_or_equal_to", "value": X}
    - greater_than: {"type": "greater_than", "value": X}
    - greater_than_or_equal_to: {"type": "greater_than_or_equal_to", "value": X}
    - equal_to: {"type": "equal_to", "value": X}
    - not_equal_to: {"type": "not_equal_to", "value": X}

    Returns (passed, issue_message).
    """
    if not condition:
        # No condition specified → trivially pass
        return True, ""

    condition_type = condition.get("type", "")

    try:
        actual = float(actual_value)
    except (TypeError, ValueError):
        return False, f"Cannot compare non-numeric value '{actual_value}' with condition"

    if condition_type == "between":
        min_val = float(condition.get("min", 0))
        max_val = float(condition.get("max", float("inf")))
        if min_val <= actual <= max_val:
            return True, ""
        return False, f"Value {actual} is not between {min_val} and {max_val}"

    elif condition_type == "less_than":
        threshold = float(condition["value"])
        if actual < threshold:
            return True, ""
        return False, f"Value {actual} is not less than {threshold}"

    elif condition_type == "less_than_or_equal_to":
        threshold = float(condition["value"])
        if actual <= threshold:
            return True, ""
        return False, f"Value {actual} is not less than or equal to {threshold}"

    elif condition_type == "greater_than":
        threshold = float(condition["value"])
        if actual > threshold:
            return True, ""
        return False, f"Value {actual} is not greater than {threshold}"

    elif condition_type == "greater_than_or_equal_to":
        threshold = float(condition["value"])
        if actual >= threshold:
            return True, ""
        return False, f"Value {actual} is not greater than or equal to {threshold}"

    elif condition_type == "equal_to":
        threshold = float(condition["value"])
        if actual == threshold:
            return True, ""
        return False, f"Value {actual} does not equal {threshold}"

    elif condition_type == "not_equal_to":
        threshold = float(condition["value"])
        if actual != threshold:
            return True, ""
        return False, f"Value {actual} equals {threshold} (expected not equal)"

    else:
        return False, f"Unknown condition type: '{condition_type}'"
