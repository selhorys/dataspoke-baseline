"""Schema compatibility rule evaluator."""

import logging
from typing import Any

from src.backend.validation.rules.helpers import RuleEvaluation
from src.backend.validation.rules.registry import register_rule
from src.shared.datahub.client import DataHubClient

logger = logging.getLogger(__name__)


@register_rule("schema")
async def evaluate(
    datahub: DataHubClient,
    dataset_urn: str,
    rule: dict[str, Any],
    partition: dict[str, Any],
    db: Any = None,
) -> RuleEvaluation:
    """Evaluate schema compatibility against expected fields/types."""
    from datahub.metadata.schema_classes import SchemaMetadataClass

    rule_id = rule.get("rule_id", "")
    expected_fields: list[dict[str, Any]] = rule.get("expected_fields", [])
    compatibility = rule.get("compatibility", "superset")  # exact_match | superset | subset

    schema: SchemaMetadataClass | None = await datahub.get_aspect(dataset_urn, SchemaMetadataClass)

    if schema is None:
        return RuleEvaluation(
            rule_id=rule_id,
            assertion_result="FAILURE",
            values={"field_count": 0},
            validation=None,
            issues=[{"msg": "No schema metadata found for dataset", "type": "no_schema"}],
            partition=partition,
        )

    actual_fields = schema.fields or []
    actual_field_map: dict[str, str] = {
        f.fieldPath: getattr(f, "nativeDataType", "") for f in actual_fields
    }

    expected_field_map: dict[str, str] = {
        f["name"]: f.get("type", "") for f in expected_fields
    }

    missing_fields = [name for name in expected_field_map if name not in actual_field_map]
    extra_fields = [name for name in actual_field_map if name not in expected_field_map]

    # Type mismatches for fields present in both
    type_mismatches = [
        {
            "field": name,
            "expected": expected_field_map[name],
            "actual": actual_field_map[name],
        }
        for name in expected_field_map
        if name in actual_field_map
        and expected_field_map[name]
        and actual_field_map[name] != expected_field_map[name]
    ]

    values: dict[str, Any] = {
        "actual_field_count": len(actual_field_map),
        "expected_field_count": len(expected_field_map),
        "missing_field_count": len(missing_fields),
        "extra_field_count": len(extra_fields),
        "type_mismatch_count": len(type_mismatches),
    }

    issues: list[dict[str, Any]] = []

    if compatibility == "exact_match":
        if missing_fields or extra_fields or type_mismatches:
            if missing_fields:
                issues.append(
                    {"msg": f"Missing fields: {missing_fields}", "type": "missing_fields"}
                )
            if extra_fields:
                issues.append(
                    {
                        "msg": f"Extra fields not in expected schema: {extra_fields}",
                        "type": "extra_fields",
                    }
                )
    elif compatibility == "superset":
        # Actual schema must contain all expected fields
        if missing_fields:
            issues.append(
                {"msg": f"Missing required fields: {missing_fields}", "type": "missing_fields"}
            )
    elif compatibility == "subset":
        # All actual fields must be in expected schema
        if extra_fields:
            issues.append(
                {"msg": f"Unexpected fields in schema: {extra_fields}", "type": "extra_fields"}
            )

    if type_mismatches:
        for tm in type_mismatches:
            issues.append(
                {
                    "msg": (
                        f"Type mismatch for field '{tm['field']}': "
                        f"expected '{tm['expected']}', got '{tm['actual']}'"
                    ),
                    "type": "type_mismatch",
                    **tm,
                }
            )

    assertion_result = "SUCCESS" if not issues else "FAILURE"
    return RuleEvaluation(
        rule_id=rule_id,
        assertion_result=assertion_result,
        values=values,
        validation=None,
        issues=issues,
        partition=partition,
    )
