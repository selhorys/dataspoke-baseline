"""DataHub assertion bridge — build URNs, emit assertion info, and report run events."""

import logging
import time
from typing import Any

from datahub.emitter.mcp_builder import datahub_guid
from datahub.metadata.schema_classes import (
    AssertionInfoClass,
    AssertionResultClass,
    AssertionResultTypeClass,
    AssertionRunEventClass,
    AssertionRunStatusClass,
    AssertionSourceClass,
    AssertionSourceTypeClass,
    AssertionTypeClass,
    PartitionSpecClass,
    PartitionTypeClass,
)

from src.shared.datahub.client import DataHubClient

logger = logging.getLogger(__name__)

# Maps DataSpoke rule type strings to DataHub AssertionTypeClass constants.
_RULE_TYPE_MAP: dict[str, str] = {
    "freshness": AssertionTypeClass.FRESHNESS,
    "volume": AssertionTypeClass.VOLUME,
    "field": AssertionTypeClass.FIELD,
    "schema": AssertionTypeClass.DATA_SCHEMA,
    "sql": AssertionTypeClass.SQL,
    "custom": AssertionTypeClass.CUSTOM,
}


def build_assertion_urn(dataset_urn: str, rule_id: str) -> str:
    """Return a deterministic assertion URN for a dataset + rule combination.

    Uses datahub_guid() so the result is a valid DataHub-style GUID and
    consistent across re-runs.
    """
    guid = datahub_guid({"entity": dataset_urn, "rule": rule_id})
    return f"urn:li:assertion:{guid}"


def build_assertion_info(dataset_urn: str, rule: dict[str, Any]) -> AssertionInfoClass:
    """Construct an AssertionInfoClass from a DataSpoke rule dict."""
    rule_type = rule.get("type", "custom").lower()
    assertion_type = _RULE_TYPE_MAP.get(rule_type, AssertionTypeClass.CUSTOM)

    source = AssertionSourceClass(
        type=AssertionSourceTypeClass.EXTERNAL,
    )

    return AssertionInfoClass(
        type=assertion_type,
        datasetAssertion=None,
        customAssertion=None,
        description=rule.get("description", f"DataSpoke {rule_type} assertion"),
        source=source,
        customProperties={
            "dataspoke_rule_id": rule.get("rule_id", ""),
            "dataspoke_rule_type": rule_type,
        },
    )


def build_run_event(
    assertion_urn: str,
    dataset_urn: str,
    run_id: str,
    result: str,
    values: dict[str, Any],
    partition: dict[str, Any],
) -> AssertionRunEventClass:
    """Construct an AssertionRunEventClass for reporting a rule evaluation result.

    Args:
        assertion_urn: The assertion URN built from build_assertion_urn().
        dataset_urn: The target dataset URN.
        run_id: The workflow execution / validation run ID.
        result: One of "SUCCESS", "FAILURE", "ERROR".
        values: Computed metric values for this run.
        partition: Partition context (empty dict = full-table scan).
    """
    result_type_map: dict[str, str] = {
        "SUCCESS": AssertionResultTypeClass.SUCCESS,
        "FAILURE": AssertionResultTypeClass.FAILURE,
        "ERROR": AssertionResultTypeClass.ERROR,
    }
    assertion_result_type = result_type_map.get(result.upper(), AssertionResultTypeClass.ERROR)

    native_results = {k: str(v) for k, v in values.items()}

    assertion_result = AssertionResultClass(
        type=assertion_result_type,
        nativeResults=native_results if native_results else None,
    )

    partition_type = PartitionTypeClass.FULL_TABLE if not partition else PartitionTypeClass.PARTITION
    partition_spec = PartitionSpecClass(
        type=partition_type,
        partition=str(partition) if partition else None,
    )

    return AssertionRunEventClass(
        assertionUrn=assertion_urn,
        asserteeUrn=dataset_urn,
        runId=run_id,
        result=assertion_result,
        status=AssertionRunStatusClass.COMPLETE,
        timestampMillis=int(time.time() * 1000),
        partitionSpec=partition_spec,
    )


async def register_assertion(
    datahub: DataHubClient,
    assertion_urn: str,
    assertion_info: AssertionInfoClass,
) -> None:
    """Emit assertion definition to DataHub if it does not already exist.

    Best-effort — logs a warning on failure but does not raise.
    """
    try:
        existing = await datahub.get_assertion_info(assertion_urn)
        if existing is not None:
            return
        await datahub.emit_assertion(assertion_urn, assertion_info)
    except Exception:
        logger.warning(
            "assertion_register_failed",
            exc_info=True,
            extra={"assertion_urn": assertion_urn},
        )


async def report_result(
    datahub: DataHubClient,
    assertion_urn: str,
    run_event: AssertionRunEventClass,
) -> None:
    """Emit an assertion run event to DataHub.

    Best-effort — logs a warning on failure but does not raise.
    """
    try:
        await datahub.emit_assertion(assertion_urn, run_event)
    except Exception:
        logger.warning(
            "assertion_report_failed",
            exc_info=True,
            extra={"assertion_urn": assertion_urn},
        )
