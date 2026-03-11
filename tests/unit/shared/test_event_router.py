"""Unit tests for the MCL EventRouter, deserialization, and handler dispatch."""

import json
from unittest.mock import AsyncMock

import pytest

from src.shared.datahub.events import (
    EventRouter,
    MetadataChangeLogEvent,
    build_router,
    check_freshness_sla,
    deserialize_mcl,
    detect_new_clusters,
    sync_vector_index,
    trigger_quality_check,
    update_health_score,
)
from src.shared.exceptions import DataHubUnavailableError, EventProcessingError

# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_event(
    *,
    entity_type: str = "dataset",
    entity_urn: str = "urn:li:dataset:(urn:li:dataPlatform:postgres,imazon.public.users,PROD)",
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
        "entityUrn": "urn:li:dataset:(urn:li:dataPlatform:postgres,imazon.public.users,PROD)",
        "aspectName": "datasetProperties",
        "changeType": "UPSERT",
        "aspect": {"value": "test"},
        "created": {"time": 1700000000000},
    }
    data.update(overrides)
    return json.dumps(data).encode()


# ── deserialize_mcl ──────────────────────────────────────────────────────────


class TestDeserializeMcl:
    def test_valid_json(self) -> None:
        event = deserialize_mcl(_make_raw_mcl())
        assert event.entity_type == "dataset"
        assert event.entity_urn.startswith("urn:li:dataset:")
        assert event.aspect_name == "datasetProperties"
        assert event.change_type == "UPSERT"
        assert event.aspect == {"value": "test"}
        assert event.created is not None

    def test_missing_optional_fields(self) -> None:
        raw = json.dumps(
            {
                "entityType": "dataset",
                "entityUrn": "urn:li:dataset:(urn:li:dataPlatform:postgres,t,PROD)",
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

    async def test_handler_timeout_swallowed(self) -> None:
        async def slow_handler(event: MetadataChangeLogEvent) -> None:
            import asyncio

            await asyncio.sleep(999)

        router = EventRouter()
        router.register("ownership", slow_handler)

        event = _make_event(aspect_name="ownership")
        # Should not raise — timeout is caught and logged
        await router.dispatch(event)


# ── Handler Stubs ────────────────────────────────────────────────────────────


class TestHandlerStubs:
    async def test_sync_vector_index_skips_non_dataset(self) -> None:
        event = _make_event(entity_type="chart", aspect_name="datasetProperties")
        # Should return silently without error
        await sync_vector_index(event)

    async def test_detect_new_clusters_skips_non_dataset(self) -> None:
        event = _make_event(entity_type="dashboard", aspect_name="schemaMetadata")
        await detect_new_clusters(event)

    async def test_update_health_score_skips_non_dataset(self) -> None:
        event = _make_event(entity_type="dataJob", aspect_name="ownership")
        await update_health_score(event)

    async def test_trigger_quality_check_skips_non_dataset(self) -> None:
        event = _make_event(entity_type="chart", aspect_name="datasetProfile")
        await trigger_quality_check(event)

    async def test_check_freshness_sla_skips_non_dataset(self) -> None:
        event = _make_event(entity_type="dataFlow", aspect_name="operation")
        await check_freshness_sla(event)

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
            "ownership",
            "datasetProfile",
            "operation",
        }
        assert set(router.registered_aspects.keys()) == expected_aspects

    def test_routing_table_completeness(self) -> None:
        router = build_router()
        handlers = router.registered_aspects

        # datasetProperties → sync_vector_index
        assert handlers["datasetProperties"] == [sync_vector_index]

        # schemaMetadata → sync_vector_index + detect_new_clusters
        assert handlers["schemaMetadata"] == [sync_vector_index, detect_new_clusters]

        # globalTags → sync_vector_index + update_health_score
        assert handlers["globalTags"] == [sync_vector_index, update_health_score]

        # ownership → update_health_score
        assert handlers["ownership"] == [update_health_score]

        # datasetProfile → trigger_quality_check
        assert handlers["datasetProfile"] == [trigger_quality_check]

        # operation → check_freshness_sla
        assert handlers["operation"] == [check_freshness_sla]
