"""DataHub assertion bridge — URN derivation, aspect builders, and emit helpers."""

import logging
import time
import uuid
from datetime import datetime

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
    AuditStampClass,
    CustomAssertionInfoClass,
    StatusClass,
)

from src.shared.datahub.client import DataHubClient

logger = logging.getLogger(__name__)

_DATASPOKE_ACTOR_URN = "urn:li:corpuser:dataspoke"


def build_assertion_urn(dataset_urn: str) -> str:
    """Return the deterministic assertion URN for a dataset.

    Recomputable from ``dataset_urn`` alone — PUT/PATCH/DELETE all reuse the same URN.
    """
    guid = datahub_guid({"platform": "dataspoke-validation", "entity": dataset_urn})
    return f"urn:li:assertion:{guid}"


def build_assertion_info(
    dataset_urn: str,
    description: str,
    variables: list[str],
) -> AssertionInfoClass:
    """Build an AssertionInfoClass for a DataSpoke validation slot."""
    return AssertionInfoClass(
        type=AssertionTypeClass.CUSTOM,
        description=description,
        source=AssertionSourceClass(type=AssertionSourceTypeClass.EXTERNAL),
        customAssertion=CustomAssertionInfoClass(
            type="DATASPOKE_VALIDATION",
            entity=dataset_urn,
            logic=", ".join(variables),
        ),
        lastUpdated=AuditStampClass(
            time=int(time.time() * 1000),
            actor=_DATASPOKE_ACTOR_URN,
        ),
    )


def build_run_event(
    assertion_urn: str,
    dataset_urn: str,
    data_time: datetime,
    score: float,
    variables: dict[str, float],
) -> AssertionRunEventClass:
    """Build an AssertionRunEventClass for a pipeline-emitted result.

    ``timestampMillis`` is derived from ``data_time`` (the time the underlying
    data is for), not from server-side now().  This aligns DataHub's chart axis
    with the user's mental model.
    """
    epoch_ms = int(data_time.timestamp() * 1000)

    result_type = (
        AssertionResultTypeClass.SUCCESS
        if score == 1.0
        else AssertionResultTypeClass.FAILURE
    )

    native_results: dict[str, str] = {k: repr(float(v)) for k, v in variables.items()}
    native_results["score"] = repr(float(score))

    return AssertionRunEventClass(
        assertionUrn=assertion_urn,
        asserteeUrn=dataset_urn,
        runId=uuid.uuid4().hex,
        result=AssertionResultClass(
            type=result_type,
            actualAggValue=score,
            nativeResults=native_results,
        ),
        status=AssertionRunStatusClass.COMPLETE,
        timestampMillis=epoch_ms,
        runtimeContext={"ingestion_time": str(int(time.time() * 1000))},
    )


async def register_assertion(
    datahub: DataHubClient,
    assertion_urn: str,
    info: AssertionInfoClass,
) -> None:
    """Emit assertionInfo and status(removed=False) to DataHub.

    Both are versioned aspects.  Errors propagate — DataHub availability is
    required at config-save time per spec (DataHub-first ordering).
    """
    await datahub.emit_assertion(assertion_urn, info)
    await datahub.emit_aspect(assertion_urn, StatusClass(removed=False))


async def tombstone_assertion(
    datahub: DataHubClient,
    assertion_urn: str,
) -> None:
    """Emit status(removed=True) to DataHub.  Errors propagate."""
    await datahub.emit_aspect(assertion_urn, StatusClass(removed=True))


async def report_result(
    datahub: DataHubClient,
    assertion_urn: str,
    run_event: AssertionRunEventClass,
) -> bool:
    """Emit an assertion run event to DataHub (best-effort).

    Returns True on success, False on failure.  Failures are logged as warnings
    with ``assertion_urn`` in extras so operators can correlate them.
    """
    try:
        await datahub.emit_assertion(assertion_urn, run_event)
        return True
    except Exception:
        logger.warning(
            "assertion_report_failed",
            exc_info=True,
            extra={"assertion_urn": assertion_urn},
        )
        return False
