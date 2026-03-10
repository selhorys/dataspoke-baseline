"""Unit tests for feature-specific API schemas."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from src.api.schemas.common import PaginationParams, TimeRangeParams
from src.api.schemas.dataset import DatasetAttributesResponse, DatasetResponse
from src.api.schemas.events import EventListResponse, EventResponse
from src.api.schemas.generation import (
    CreateGenerationConfigRequest,
    GenerationConfigListResponse,
    GenerationConfigResponse,
)
from src.api.schemas.ingestion import (
    CreateIngestionConfigRequest,
    IngestionConfigListResponse,
    IngestionConfigResponse,
    RunResultResponse,
)
from src.api.schemas.metrics import (
    MetricDefinitionListResponse,
    MetricDefinitionResponse,
    UpsertMetricConfigRequest,
)
from src.api.schemas.ontology import ConceptListResponse, ConceptResponse
from src.api.schemas.overview import OverviewResponse
from src.api.schemas.search import SearchResponse, SearchResultItem
from src.api.schemas.validation import (
    CreateValidationConfigRequest,
    ValidationConfigListResponse,
    ValidationConfigResponse,
)


class TestPaginationParams:
    def test_defaults(self) -> None:
        p = PaginationParams()
        assert p.offset == 0
        assert p.limit == 20
        assert p.sort is None

    def test_rejects_negative_offset(self) -> None:
        with pytest.raises(ValidationError):
            PaginationParams(offset=-1)

    def test_rejects_zero_limit(self) -> None:
        with pytest.raises(ValidationError):
            PaginationParams(limit=0)

    def test_rejects_limit_over_100(self) -> None:
        with pytest.raises(ValidationError):
            PaginationParams(limit=101)

    def test_accepts_max_limit(self) -> None:
        p = PaginationParams(limit=100)
        assert p.limit == 100


class TestTimeRangeParams:
    def test_defaults_to_none(self) -> None:
        t = TimeRangeParams()
        assert t.from_time is None
        assert t.to_time is None

    def test_alias_from(self) -> None:
        now = datetime.now(tz=UTC)
        t = TimeRangeParams(**{"from": now})
        assert t.from_time == now


class TestIngestionSchemas:
    def test_create_request_round_trip(self) -> None:
        req = CreateIngestionConfigRequest(
            dataset_urn="urn:li:dataset:test",
            sources={"type": "postgres"},
            owner="admin",
        )
        data = req.model_dump()
        parsed = CreateIngestionConfigRequest.model_validate(data)
        assert parsed.dataset_urn == req.dataset_urn
        assert parsed.deep_spec_enabled is False

    def test_config_response_has_resp_time(self) -> None:
        resp = IngestionConfigResponse(
            id="1",
            dataset_urn="urn:li:dataset:test",
            sources={},
            deep_spec_enabled=False,
            schedule=None,
            status="active",
            owner="admin",
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )
        assert resp.resp_time is not None

    def test_list_response_has_pagination_fields(self) -> None:
        resp = IngestionConfigListResponse(total_count=5)
        assert resp.offset == 0
        assert resp.limit == 20
        assert resp.total_count == 5
        assert resp.configs == []

    def test_run_result_response(self) -> None:
        resp = RunResultResponse(run_id="r1", status="started")
        assert resp.run_id == "r1"
        assert resp.detail == {}


class TestValidationSchemas:
    def test_create_request(self) -> None:
        req = CreateValidationConfigRequest(
            dataset_urn="urn:li:dataset:test",
            rules={"null_check": True},
            owner="admin",
        )
        assert req.schedule is None

    def test_config_response_round_trip(self) -> None:
        now = datetime.now(tz=UTC)
        resp = ValidationConfigResponse(
            id="1",
            dataset_urn="urn:li:dataset:test",
            rules={},
            schedule=None,
            sla_target=None,
            status="active",
            owner="admin",
            created_at=now,
            updated_at=now,
        )
        data = resp.model_dump()
        parsed = ValidationConfigResponse.model_validate(data)
        assert parsed.id == "1"

    def test_list_response(self) -> None:
        resp = ValidationConfigListResponse()
        assert resp.configs == []


class TestGenerationSchemas:
    def test_create_request(self) -> None:
        req = CreateGenerationConfigRequest(
            dataset_urn="urn:li:dataset:test",
            target_fields={"description": True, "tags": True},
            owner="admin",
        )
        assert req.code_refs is None

    def test_config_response(self) -> None:
        now = datetime.now(tz=UTC)
        resp = GenerationConfigResponse(
            id="1",
            dataset_urn="urn:li:dataset:test",
            target_fields={"description": True},
            code_refs=None,
            schedule=None,
            status="active",
            owner="admin",
            created_at=now,
            updated_at=now,
        )
        assert resp.resp_time is not None

    def test_list_response(self) -> None:
        resp = GenerationConfigListResponse()
        assert resp.configs == []


class TestSearchSchemas:
    def test_search_result_item(self) -> None:
        item = SearchResultItem(
            urn="urn:li:dataset:test",
            name="test",
            platform="postgres",
            score=0.95,
            owners=["urn:li:corpuser:alice"],
            quality_score=85,
        )
        assert item.owners == ["urn:li:corpuser:alice"]
        assert item.quality_score == 85
        assert item.sql_context is None

    def test_search_response(self) -> None:
        item = SearchResultItem(
            urn="urn:li:dataset:test",
            name="test",
            platform="postgres",
            score=0.95,
        )
        resp = SearchResponse(datasets=[item], total_count=1)
        assert len(resp.datasets) == 1
        assert resp.total_count == 1


class TestOntologySchemas:
    def test_concept_response(self) -> None:
        now = datetime.now(tz=UTC)
        resp = ConceptResponse(
            id="c1",
            name="PII",
            description="Personally identifiable information",
            parent_id=None,
            status="approved",
            version=1,
            created_at=now,
            updated_at=now,
        )
        assert resp.resp_time is not None

    def test_concept_list_response(self) -> None:
        resp = ConceptListResponse()
        assert resp.concepts == []


class TestDatasetSchemas:
    def test_dataset_response(self) -> None:
        resp = DatasetResponse(
            urn="urn:li:dataset:test",
            name="test",
            platform="postgres",
        )
        assert resp.owners == []
        assert resp.tags == []

    def test_attributes_response(self) -> None:
        resp = DatasetAttributesResponse(
            urn="urn:li:dataset:test",
            column_count=10,
        )
        assert resp.quality_score is None


class TestMetricsSchemas:
    def test_upsert_request(self) -> None:
        req = UpsertMetricConfigRequest(
            title="Row Count",
            description="Counts total rows",
            theme="quality",
            measurement_query={"type": "dataset_count"},
        )
        assert req.alarm_enabled is False
        assert req.active is True

    def test_definition_response(self) -> None:
        now = datetime.now(tz=UTC)
        resp = MetricDefinitionResponse(
            id="m1",
            title="Row Count",
            description="Counts total rows",
            theme="quality",
            measurement_query={"type": "dataset_count"},
            schedule=None,
            alarm_enabled=False,
            alarm_threshold=None,
            active=True,
            created_at=now,
            updated_at=now,
        )
        assert resp.resp_time is not None

    def test_list_response(self) -> None:
        resp = MetricDefinitionListResponse()
        assert resp.metrics == []


class TestOverviewSchemas:
    def test_overview_defaults(self) -> None:
        resp = OverviewResponse()
        assert resp.layout == "grid"
        assert resp.color_by == "quality"
        assert resp.stats.total_datasets == 0


class TestEventSchemas:
    def test_event_response(self) -> None:
        now = datetime.now(tz=UTC)
        resp = EventResponse(
            id="e1",
            entity_type="dataset",
            entity_id="d1",
            event_type="ingestion_run",
            status="success",
            occurred_at=now,
        )
        assert resp.detail == {}

    def test_event_list_response(self) -> None:
        resp = EventListResponse()
        assert resp.events == []
        assert resp.total_count == 0
