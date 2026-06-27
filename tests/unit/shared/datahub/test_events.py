"""Unit tests for shared datahub events module (events.py).

Tests the MetadataChangeLogEvent model and EventRouter:
- MetadataChangeLogEvent required fields are enforced.
- MetadataChangeLogEvent optional fields default correctly.
- EventRouter.registered_aspects reflects wired handlers.
- deserialize_mcl round-trip: JSON → model with correct field mapping.

spec: DATAHUB_INTEGRATION.md §Event Subscription — MCL envelope fields (entity_urn,
      aspect_name, etc.) are the upstream DataHub wire format; entity_type, entity_urn,
      aspect_name, change_type are required; aspect, created are optional.
      Handler exception-swallowing semantics and retryable-error propagation are impl
      conventions with no baseline spec text.
"""

import json

import pytest

from src.shared.datahub.events import (
    EventRouter,
    MetadataChangeLogEvent,
    deserialize_mcl,
)


# ── MetadataChangeLogEvent model ──────────────────────────────────────────────


def test_mcl_event_required_fields_enforced() -> None:
    """MetadataChangeLogEvent must require entity_type, entity_urn, aspect_name, change_type.

    spec: DATAHUB_INTEGRATION.md §Event Subscription — required fields.
    """
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        MetadataChangeLogEvent()  # type: ignore[call-arg]  # missing all required fields


def test_mcl_event_optional_fields_default_to_none() -> None:
    """MetadataChangeLogEvent.aspect and .created default to None.

    spec: DATAHUB_INTEGRATION.md §Event Subscription — aspect, created are optional.
    """
    event = MetadataChangeLogEvent(
        entity_type="dataset",
        entity_urn="urn:li:dataset:test",
        aspect_name="datasetProperties",
        change_type="UPSERT",
    )
    assert event.aspect is None
    assert event.created is None


def test_mcl_event_stores_aspect_dict() -> None:
    """MetadataChangeLogEvent.aspect stores the provided dict.

    spec: DATAHUB_INTEGRATION.md §Event Subscription — aspect carries the changed aspect value.
    """
    aspect_data = {"description": "A new table description"}
    event = MetadataChangeLogEvent(
        entity_type="dataset",
        entity_urn="urn:li:dataset:test",
        aspect_name="datasetProperties",
        change_type="UPSERT",
        aspect=aspect_data,
    )
    assert event.aspect == aspect_data


# ── Round-trip: deserialize_mcl → MetadataChangeLogEvent ─────────────────────


def test_deserialize_mcl_round_trip_all_fields() -> None:
    """deserialize_mcl produces a MetadataChangeLogEvent with all fields populated.

    spec: DATAHUB_INTEGRATION.md §Event Subscription — full field mapping.
    """
    payload = {
        "entityType": "dataset",
        "entityUrn": "urn:li:dataset:(urn:li:dataPlatform:postgres,db.orders,DEV)",
        "aspectName": "schemaMetadata",
        "changeType": "UPSERT",
        "aspect": {"fields": []},
        "created": {"time": 999, "actor": "urn:li:corpuser:datahub"},
    }
    event = deserialize_mcl(json.dumps(payload).encode())

    assert event.entity_type == "dataset"
    assert event.entity_urn == "urn:li:dataset:(urn:li:dataPlatform:postgres,db.orders,DEV)"
    assert event.aspect_name == "schemaMetadata"
    assert event.change_type == "UPSERT"
    assert event.aspect == {"fields": []}
    assert event.created == {"time": 999, "actor": "urn:li:corpuser:datahub"}


# ── EventRouter: registered_aspects ──────────────────────────────────────────


def test_event_router_registered_aspects_is_dict() -> None:
    """EventRouter.registered_aspects returns a dict mapping aspect names to handler lists.

    spec: DATAHUB_INTEGRATION.md §Event Subscription — routing table is accessible.
    """
    router = EventRouter()
    assert isinstance(router.registered_aspects, dict)


def test_event_router_register_adds_to_registered_aspects() -> None:
    """EventRouter.register adds handler to the registered_aspects mapping.

    spec: DATAHUB_INTEGRATION.md §Event Subscription — router.register wires handler.
    """
    async def _handler(event: MetadataChangeLogEvent) -> None:
        pass

    router = EventRouter()
    router.register("myAspect", _handler)

    aspects = router.registered_aspects
    assert "myAspect" in aspects
    assert _handler in aspects["myAspect"]


