"""Unit tests for the DataHub MCL event consumer (consumer.py).

Tests spec-grounded behavior:
- MCL_TOPICS contains the two documented topic names.
- deserialize_mcl parses the documented MCL JSON envelope (camelCase → snake_case).
- deserialize_mcl raises EventProcessingError on invalid JSON.
- deserialize_mcl raises EventProcessingError on non-JSON bytes.
- EventRouter dispatches to handlers registered for the event's aspect_name.
- EventRouter does not dispatch to handlers for other aspect names.

spec: DATAHUB_INTEGRATION.md §Event Subscription — topic names
      MetadataChangeLog_Versioned_v1 and MetadataChangeLog_Timeseries_v1 are documented;
      MCL envelope fields (entity_urn, aspect_name, etc.) are the upstream DataHub wire
      format. Handler dispatch ordering and build_router aspect wiring are impl conventions
      with no baseline spec text.
"""

import json

import pytest

from src.shared.datahub.consumer import MCL_TOPICS
from src.shared.datahub.events import (
    EventRouter,
    MetadataChangeLogEvent,
    deserialize_mcl,
)
from src.shared.exceptions import EventProcessingError

# ── MCL_TOPICS ────────────────────────────────────────────────────────────────


def test_mcl_topics_contains_versioned_and_timeseries() -> None:
    """MCL_TOPICS must include both versioned and timeseries topic names.

    spec: DATAHUB_INTEGRATION.md §Event Subscription — subscribed topics.
    """
    assert "MetadataChangeLog_Versioned_v1" in MCL_TOPICS, (
        "MCL_TOPICS must include 'MetadataChangeLog_Versioned_v1'"
    )
    assert "MetadataChangeLog_Timeseries_v1" in MCL_TOPICS, (
        "MCL_TOPICS must include 'MetadataChangeLog_Timeseries_v1'"
    )


# ── deserialize_mcl ───────────────────────────────────────────────────────────


def test_deserialize_mcl_maps_camelcase_to_snake_case() -> None:
    """deserialize_mcl maps DataHub camelCase fields to snake_case model attributes.

    spec: DATAHUB_INTEGRATION.md §Event Subscription — MCL envelope camelCase fields.
    """
    payload = {
        "entityType": "dataset",
        "entityUrn": "urn:li:dataset:test",
        "aspectName": "schemaMetadata",
        "changeType": "UPSERT",
        "aspect": {"value": "v1"},
        "created": {"time": 1234},
    }
    raw = json.dumps(payload).encode()

    event = deserialize_mcl(raw)

    assert isinstance(event, MetadataChangeLogEvent)
    assert event.entity_type == "dataset"
    assert event.entity_urn == "urn:li:dataset:test"
    assert event.aspect_name == "schemaMetadata"
    assert event.change_type == "UPSERT"
    assert event.aspect == {"value": "v1"}
    assert event.created == {"time": 1234}


def test_deserialize_mcl_missing_optional_fields_defaults_to_none() -> None:
    """deserialize_mcl must not raise when optional fields (aspect, created) are absent.

    spec: DATAHUB_INTEGRATION.md §Event Subscription — aspect and created are optional.
    """
    payload = {
        "entityType": "dataset",
        "entityUrn": "urn:li:dataset:minimal",
        "aspectName": "datasetProperties",
        "changeType": "UPSERT",
    }
    raw = json.dumps(payload).encode()
    event = deserialize_mcl(raw)

    assert event.aspect is None
    assert event.created is None


def test_deserialize_mcl_raises_on_invalid_json() -> None:
    """deserialize_mcl must raise EventProcessingError on invalid JSON bytes.

    spec: DATAHUB_INTEGRATION.md §Event Subscription — malformed messages are skipped
          (offset committed) after raising EventProcessingError.
    """
    with pytest.raises(EventProcessingError):
        deserialize_mcl(b"not-valid-json{{{")


def test_deserialize_mcl_raises_on_non_bytes_like() -> None:
    """deserialize_mcl must raise EventProcessingError on None input.

    spec: DATAHUB_INTEGRATION.md §Event Subscription — error handling for corrupt messages.
    """
    with pytest.raises(EventProcessingError):
        deserialize_mcl(None)  # type: ignore[arg-type]


# ── EventRouter ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_event_router_dispatches_to_registered_handler() -> None:
    """EventRouter.dispatch calls handlers registered for the event's aspect_name.

    spec: DATAHUB_INTEGRATION.md §Event Subscription — event routed to registered handler.
    """
    called: list[str] = []

    async def _handler(event: MetadataChangeLogEvent) -> None:
        called.append(event.aspect_name)

    router = EventRouter()
    router.register("schemaMetadata", _handler)

    event = MetadataChangeLogEvent(
        entity_type="dataset",
        entity_urn="urn:li:dataset:test",
        aspect_name="schemaMetadata",
        change_type="UPSERT",
    )
    await router.dispatch(event)

    assert called == ["schemaMetadata"], (
        "Handler must be called for the matching aspect_name."
    )


@pytest.mark.asyncio
async def test_event_router_does_not_dispatch_to_other_aspect_handlers() -> None:
    """EventRouter does not dispatch when aspect_name does not match registered handler.

    spec: DATAHUB_INTEGRATION.md §Event Subscription — routing is by aspect_name.
    """
    called: list[str] = []

    async def _handler(event: MetadataChangeLogEvent) -> None:
        called.append(event.aspect_name)

    router = EventRouter()
    router.register("globalTags", _handler)

    event = MetadataChangeLogEvent(
        entity_type="dataset",
        entity_urn="urn:li:dataset:test",
        aspect_name="schemaMetadata",  # different from registered
        change_type="UPSERT",
    )
    await router.dispatch(event)

    assert called == [], (
        "Handler for 'globalTags' must not be called for 'schemaMetadata' event."
    )


@pytest.mark.asyncio
async def test_event_router_propagates_peripheral_not_configured() -> None:
    """A handler raising PeripheralNotConfiguredError propagates out of dispatch.

    DataHub fails closed: an unconfigured DataHub encountered while processing a
    Kafka MCL event must NOT be swallowed like a generic handler exception. It
    propagates so the consumer skips the offset commit and the message is retried,
    rather than silently dropping the event.

    spec: spec/ARCHITECTURE.md §Peripheral availability contract — DataHub fails
          closed: any path requiring DataHub surfaces an error rather than a silent
          or partial success.
    """
    from src.shared.exceptions import PeripheralNotConfiguredError

    async def _handler(event: MetadataChangeLogEvent) -> None:
        raise PeripheralNotConfiguredError("datahub")

    router = EventRouter()
    router.register("schemaMetadata", _handler)

    event = MetadataChangeLogEvent(
        entity_type="dataset",
        entity_urn="urn:li:dataset:test",
        aspect_name="schemaMetadata",
        change_type="UPSERT",
    )

    with pytest.raises(PeripheralNotConfiguredError) as exc_info:
        await router.dispatch(event)

    assert exc_info.value.detail["peripheral"] == "datahub"


@pytest.mark.asyncio
async def test_event_router_swallows_generic_handler_exception() -> None:
    """A handler raising a generic exception is swallowed (logged) by dispatch.

    Contrast with the retryable PeripheralNotConfiguredError path: a generic
    failure does not propagate, so dispatch returns normally and the consumer
    commits the offset. This proves the retryable re-raise tuple is selective.

    spec: spec/ARCHITECTURE.md §Peripheral availability contract — only fail-closed
          / storage-tier errors are retryable; other handler failures are skipped.
    """
    invoked = False

    async def _handler(event: MetadataChangeLogEvent) -> None:
        nonlocal invoked
        invoked = True
        raise RuntimeError("non-retryable handler bug")

    router = EventRouter()
    router.register("schemaMetadata", _handler)

    event = MetadataChangeLogEvent(
        entity_type="dataset",
        entity_urn="urn:li:dataset:test",
        aspect_name="schemaMetadata",
        change_type="UPSERT",
    )

    # Must NOT raise — generic exceptions are caught and logged.
    await router.dispatch(event)

    # The handler ran (so the swallow path was exercised, not a no-op).
    assert invoked is True


