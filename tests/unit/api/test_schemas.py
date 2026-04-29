"""Unit tests for feature-specific API schemas."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from src.api.schemas.common import PaginationParams, TimeRangeParams
from src.api.schemas.dataset import DatasetAttributesResponse, DatasetResponse
from src.api.schemas.events import EventListResponse, EventResponse
from src.api.schemas.ingestion import (
    CreateIngestionConfigRequest,
    IngestionConfigListResponse,
    IngestionConfigResponse,
    RunResultResponse,
)
from src.api.schemas.metagen import (
    MetagenConfPutRequest,
    MetagenListResponse,
    MetagenResultListResponse,
)
from src.api.schemas.metrics import (
    MetricDefinitionListResponse,
    MetricDefinitionResponse,
    UpsertMetricConfigRequest,
)
from src.api.schemas.ontogen import (
    NodeListResponse,
    OntogenConfPutRequest,
    SeedListResponse,
)
from src.api.schemas.overview import OverviewResponse
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
            platform="postgres",
            locator={"host": "localhost", "port": 5432},
            identifier={"database": "testdb"},
            auth={"username": "user", "secret_ref": "pw"},
        )
        data = req.model_dump()
        parsed = CreateIngestionConfigRequest.model_validate(data)
        assert parsed.dataset_urn == req.dataset_urn
        assert parsed.is_enabled is False

    def test_create_request_kafka_no_auth(self) -> None:
        req = CreateIngestionConfigRequest(
            dataset_urn="urn:li:dataset:test",
            platform="kafka",
            locator={"bootstrap_servers": "kafka:9092"},
            identifier={"topic": "my-topic"},
        )
        assert req.auth is None

    def test_create_request_invalid_platform(self) -> None:
        with pytest.raises(ValidationError):
            CreateIngestionConfigRequest(
                dataset_urn="urn:li:dataset:test",
                platform="unsupported",
                locator={},
                identifier={},
            )

    def test_create_request_missing_auth_for_postgresql(self) -> None:
        with pytest.raises(ValidationError, match="auth is required"):
            CreateIngestionConfigRequest(
                dataset_urn="urn:li:dataset:test",
                platform="postgres",
                locator={"host": "localhost", "port": 5432},
                identifier={"database": "testdb"},
            )

    def test_config_response_has_resp_time(self) -> None:
        resp = IngestionConfigResponse(
            id="1",
            dataset_urn="urn:li:dataset:test",
            platform="postgres",
            locator={"host": "localhost", "port": 5432},
            identifier={"database": "testdb"},
            auth={"username": "user", "secret_ref": "pw"},
            is_enabled=False,
            mode="active",
            schedule_tier=None,
            workflow_dag_id=None,
            status="OK",
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
            rules=[{"rule_id": "r1", "type": "freshness", "lookback_interval": "24h"}],
            owner="admin",
        )
        assert req.schedule_tier is None

    def test_create_request_with_schedule(self) -> None:
        req = CreateValidationConfigRequest(
            dataset_urn="urn:li:dataset:test",
            rules=[],
            schedule_tier="daily",
            owner="admin",
        )
        assert req.schedule_tier == "daily"

    def test_config_response_round_trip(self) -> None:
        now = datetime.now(tz=UTC)
        resp = ValidationConfigResponse(
            id="1",
            dataset_urn="urn:li:dataset:test",
            rules=[{"rule_id": "r1", "type": "volume"}],
            schedule_tier=None,
            is_enabled=False,
            owner="admin",
            created_at=now,
            updated_at=now,
        )
        data = resp.model_dump()
        parsed = ValidationConfigResponse.model_validate(data)
        assert parsed.id == "1"
        assert parsed.rules[0]["rule_id"] == "r1"

    def test_list_response(self) -> None:
        resp = ValidationConfigListResponse()
        assert resp.configs == []


class TestMetagenSchemas:
    def test_put_request_valid_targets(self) -> None:
        req = MetagenConfPutRequest(
            dataset_urn="urn:li:dataset:test",
            targets=["dataset.description", "column.description"],
            owner="admin",
        )
        assert "dataset.description" in req.targets

    def test_put_request_invalid_target(self) -> None:
        with pytest.raises(ValidationError):
            MetagenConfPutRequest(
                dataset_urn="urn:li:dataset:test",
                targets=["invalid.field"],
                owner="admin",
            )

    def test_list_response(self) -> None:
        resp = MetagenListResponse()
        assert resp.results == []
        assert resp.total_count == 0

    def test_result_list_response(self) -> None:
        resp = MetagenResultListResponse()
        assert resp.results == []


class TestOntogenSchemas:
    def test_conf_put_request(self) -> None:
        req = OntogenConfPutRequest(
            default_run_prompt="## Ontology\nBuild a taxonomy.",
            is_enabled=True,
        )
        assert req.is_enabled is True

    def test_seed_list_response(self) -> None:
        resp = SeedListResponse(seeds=[])
        assert resp.seeds == []

    def test_node_list_response(self) -> None:
        resp = NodeListResponse()
        assert resp.nodes == []


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
            measurement_query={"type": "poorly_documented"},
            is_enabled=False,
        )
        assert req.is_enabled is False

    def test_upsert_request_enabled_with_schedule(self) -> None:
        req = UpsertMetricConfigRequest(
            title="Row Count",
            description="Counts total rows",
            theme="quality",
            measurement_query={"type": "poorly_documented"},
            is_enabled=True,
            schedule_tier="daily",
        )
        assert req.is_enabled is True
        assert req.schedule_tier == "daily"

    def test_upsert_request_enabled_without_schedule_ok(self) -> None:
        req = UpsertMetricConfigRequest(
            title="Row Count",
            description="Counts total rows",
            theme="quality",
            measurement_query={"type": "poorly_documented"},
            is_enabled=True,
        )
        assert req.is_enabled is True
        assert req.schedule_tier is None

    def test_definition_response(self) -> None:
        now = datetime.now(tz=UTC)
        resp = MetricDefinitionResponse(
            id="m1",
            title="Row Count",
            description="Counts total rows",
            theme="quality",
            measurement_query={"type": "poorly_documented"},
            schedule_tier=None,
            is_enabled=True,
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
        assert resp.layout == "force"
        assert resp.color_by == "quality_score"
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
