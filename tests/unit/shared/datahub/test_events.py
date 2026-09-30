"""Tests for src/shared/datahub/events.py — MCL EventRouter, deserialization, and
handler dispatch. Covers spec/DATAHUB_INTEGRATION.md §Event Subscription.

NOTE — TestBuildRouter: the reference handler set (datasetProperties, schemaMetadata,
globalTags → sync_vector_index / detect_new_clusters) is an implementation detail
that is NOT part of the baseline contract. Per spec/DATAHUB_INTEGRATION.md §Event Subscription:
'The baseline UC1–UC5 flows are schedule-driven via Airflow tier DAGs and do not
subscribe to DataHub's Kafka topics.' These tests pin the current impl; treat handler
registration changes as an expected revision point, not a regression."""

import json
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from src.shared.datahub.events import (
    EventRouter,
    MetadataChangeLogEvent,
    build_router,
    deserialize_mcl,
    detect_new_clusters,
    sync_vector_index,
)
from src.shared.exceptions import DataHubUnavailableError, EventProcessingError

# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_event(
    *,
    entity_type: str = "dataset",
    entity_urn: str = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)",  # noqa: E501 URN literal
    aspect_name: str = "datasetProperties",
    change_type: str = "UPSERT",
) -> MetadataChangeLogEvent:
    return MetadataChangeLogEvent(
        entity_type=entity_type,
        entity_urn=entity_urn,
        aspect_name=aspect_name,
        change_type=change_type,
    )


def _make_raw_mcl(**overrides: object) -> bytes:
    data = {
        "entityType": "dataset",
        "entityUrn": "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)",  # noqa: E501 URN literal
        "aspectName": "datasetProperties",
        "changeType": "UPSERT",
        "aspect": {"value": "test"},
        "created": {"time": 1700000000000},
    }
    data.update(overrides)
    return json.dumps(data).encode()


# ── MetadataChangeLogEvent model contract ───────────────────────────────────
#
# The required/optional field split asserted below is not itself spec text —
# spec/DATAHUB_INTEGRATION.md §Event Subscription (L1003-1018) specifies only
# routing by `aspectName`, never the envelope's field list. These tests pin
# the current model shape as an impl-detail contract so a field becoming
# silently optional (or vice versa) does not slip through `deserialize_mcl`,
# whose `data.get(key, "")` / `data.get(key)` defaults would otherwise mask
# the change end-to-end.


class TestMetadataChangeLogEventModel:
    def test_missing_all_required_fields_raises(self) -> None:
        with pytest.raises(ValidationError):
            MetadataChangeLogEvent()  # type: ignore[call-arg]

    def test_missing_entity_type_raises(self) -> None:
        with pytest.raises(ValidationError):
            MetadataChangeLogEvent(  # type: ignore[call-arg]
                entity_urn="urn:li:dataset:test",
                aspect_name="datasetProperties",
                change_type="UPSERT",
            )

    def test_missing_entity_urn_raises(self) -> None:
        with pytest.raises(ValidationError):
            MetadataChangeLogEvent(  # type: ignore[call-arg]
                entity_type="dataset",
                aspect_name="datasetProperties",
                change_type="UPSERT",
            )

    def test_missing_aspect_name_raises(self) -> None:
        with pytest.raises(ValidationError):
            MetadataChangeLogEvent(  # type: ignore[call-arg]
                entity_type="dataset",
                entity_urn="urn:li:dataset:test",
                change_type="UPSERT",
            )

    def test_missing_change_type_raises(self) -> None:
        with pytest.raises(ValidationError):
            MetadataChangeLogEvent(  # type: ignore[call-arg]
                entity_type="dataset",
                entity_urn="urn:li:dataset:test",
                aspect_name="datasetProperties",
            )

    def test_optional_fields_default_to_none(self) -> None:
        event = MetadataChangeLogEvent(
            entity_type="dataset",
            entity_urn="urn:li:dataset:test",
            aspect_name="datasetProperties",
            change_type="UPSERT",
        )
        assert event.aspect is None
        assert event.created is None


# ── deserialize_mcl ──────────────────────────────────────────────────────────


class TestDeserializeMcl:
    def test_valid_json(self) -> None:
        raw_entity_urn = (
            "urn:li:dataset:(urn:li:dataPlatform:postgres,"
            "example_db.catalog.title_master,DEV)"
        )
        event = deserialize_mcl(_make_raw_mcl())
        # Exact equality (not `is not None` / `startswith`) on every field so a
        # key swap between any two of the six `data.get(...)` mappings in
        # `deserialize_mcl` (e.g. `created=data.get("aspect")`) is caught —
        # `aspect` and `created` hold distinct dict shapes precisely so a swap
        # cannot pass by coincidence.
        assert event.entity_type == "dataset"
        assert event.entity_urn == raw_entity_urn
        assert event.aspect_name == "datasetProperties"
        assert event.change_type == "UPSERT"
        assert event.aspect == {"value": "test"}
        assert event.created == {"time": 1700000000000}

    def test_missing_optional_fields(self) -> None:
        raw = json.dumps(
            {
                "entityType": "dataset",
                "entityUrn": "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.reviews.user_ratings,DEV)",  # noqa: E501 URN literal
                "aspectName": "ownership",
                "changeType": "UPSERT",
            }
        ).encode()
        event = deserialize_mcl(raw)
        assert event.aspect is None
        assert event.created is None

    def test_invalid_json_raises(self) -> None:
        with pytest.raises(EventProcessingError, match="invalid MCL JSON"):
            deserialize_mcl(b"not-json{{{")

    def test_none_bytes_raises(self) -> None:
        with pytest.raises(EventProcessingError):
            deserialize_mcl(None)  # type: ignore[arg-type]


# ── EventRouter ──────────────────────────────────────────────────────────────


class TestEventRouter:
    async def test_register_and_dispatch_single_handler(self) -> None:
        handler = AsyncMock()
        router = EventRouter()
        router.register("datasetProperties", handler)

        event = _make_event(aspect_name="datasetProperties")
        await router.dispatch(event)

        handler.assert_awaited_once_with(event)

    async def test_dispatch_multiple_handlers_same_aspect(self) -> None:
        handler_a = AsyncMock()
        handler_b = AsyncMock()
        router = EventRouter()
        router.register("schemaMetadata", handler_a)
        router.register("schemaMetadata", handler_b)

        event = _make_event(aspect_name="schemaMetadata")
        await router.dispatch(event)

        handler_a.assert_awaited_once_with(event)
        handler_b.assert_awaited_once_with(event)

    async def test_dispatch_no_matching_handler(self) -> None:
        router = EventRouter()
        event = _make_event(aspect_name="unknownAspect")
        # Should not raise
        await router.dispatch(event)

    async def test_retryable_exception_propagates(self) -> None:
        handler = AsyncMock(side_effect=DataHubUnavailableError("unavailable"))
        router = EventRouter()
        router.register("ownership", handler)

        event = _make_event(aspect_name="ownership")
        with pytest.raises(DataHubUnavailableError):
            await router.dispatch(event)

    async def test_non_retryable_exception_swallowed(self) -> None:
        handler = AsyncMock(side_effect=ValueError("bad data"))
        router = EventRouter()
        router.register("ownership", handler)

        event = _make_event(aspect_name="ownership")
        # Non-retryable exceptions are logged but swallowed
        await router.dispatch(event)

    async def test_handler_timeout_swallowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import src.shared.datahub.events as _events_mod

        monkeypatch.setattr(_events_mod, "HANDLER_TIMEOUT_S", 0.05)

        async def slow_handler(event: MetadataChangeLogEvent) -> None:
            import asyncio

            await asyncio.sleep(999)

        router = EventRouter()
        router.register("ownership", slow_handler)

        event = _make_event(aspect_name="ownership")
        # Should not raise — timeout is caught and logged
        await router.dispatch(event)


# ── Handler Entity-Type Filtering ────────────────────────────────────────────


class TestHandlerEntityTypeFiltering:
    async def test_sync_vector_index_skips_non_dataset(self) -> None:
        event = _make_event(entity_type="chart", aspect_name="datasetProperties")
        # Should return silently without error
        await sync_vector_index(event)

    async def test_detect_new_clusters_skips_non_dataset(self) -> None:
        event = _make_event(entity_type="dashboard", aspect_name="schemaMetadata")
        await detect_new_clusters(event)

    async def test_handler_runs_for_dataset(self) -> None:
        event = _make_event(entity_type="dataset", aspect_name="datasetProperties")
        # Should not raise
        await sync_vector_index(event)


# ── build_router ─────────────────────────────────────────────────────────────


class TestBuildRouter:
    def test_registers_all_aspects(self) -> None:
        router = build_router()
        expected_aspects = {
            "datasetProperties",
            "schemaMetadata",
            "globalTags",
        }
        assert set(router.registered_aspects.keys()) == expected_aspects

    def test_routing_table_completeness(self) -> None:
        router = build_router()
        handlers = router.registered_aspects

        # datasetProperties → sync_vector_index
        assert handlers["datasetProperties"] == [sync_vector_index]

        # schemaMetadata → sync_vector_index + detect_new_clusters
        assert handlers["schemaMetadata"] == [sync_vector_index, detect_new_clusters]

        # globalTags → sync_vector_index only (prior aggregator handler removed)
        assert handlers["globalTags"] == [sync_vector_index]
