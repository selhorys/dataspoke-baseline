"""Unit tests for feature-specific API schemas (non-ingestion)."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from src.api.schemas.common import PaginationParams, TimeRangeParams
from src.api.schemas.dataset import DatasetAttributesResponse, DatasetResponse
from src.api.schemas.events import EventListResponse, EventResponse
from src.api.schemas.metrics import (
    MetricDefinitionListResponse,
    MetricDefinitionResponse,
    ReplaceMetricConfigRequest,
)
from src.api.schemas.ontogen import (
    NodeListResponse,
    OntogenConfPutRequest,
    SeedListResponse,
)
from src.api.schemas.validation import (
    PutValidationConfRequest,
    ValidationListResponse,
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

    def test_rejects_limit_over_1000(self) -> None:
        with pytest.raises(ValidationError):
            PaginationParams(limit=1001)

    def test_accepts_max_limit(self) -> None:
        p = PaginationParams(limit=1000)
        assert p.limit == 1000


class TestTimeRangeParams:
    def test_defaults_to_none(self) -> None:
        t = TimeRangeParams()
        assert t.from_time is None
        assert t.to_time is None

    def test_alias_from(self) -> None:
        now = datetime.now(tz=UTC)
        t = TimeRangeParams(**{"from": now})
        assert t.from_time == now


class TestValidationSchemas:
    """Passive result-store validation schema smoke tests.

    spec: VALIDATION.md §Rule Configuration, §Validation Result.
    Full constraint tests are in tests/unit/api/test_validation_schemas.py.
    """

    def test_put_request_accepted(self) -> None:
        # spec: VALIDATION.md §Rule Configuration — description + variables required;
        # each variable is a {name, description} object.
        req = PutValidationConfRequest(
            description="Daily row count check",
            variables=[
                {"name": "row_cnt", "description": "Daily row count"},
                {"name": "null_rate", "description": ""},
            ],
        )
        assert req.description == "Daily row count check"
        assert [v.name for v in req.variables] == ["row_cnt", "null_rate"]

    def test_list_response_default_empty(self) -> None:
        # spec: VALIDATION.md §API Surface — cross-dataset list
        # spec: API.md §Standard Response Envelope — content key named after the resource
        resp = ValidationListResponse()
        assert resp.validations == []


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
    def test_replace_request(self) -> None:
        """ReplaceMetricConfigRequest accepts the full field set (mode, metric_type, etc).

        Spec: spec/API.md §Metric — PUT /spoke/governance/metric/{id}/attr/conf fields.
        """
        req = ReplaceMetricConfigRequest(
            mode="active",
            is_enabled=False,
            metric_type="ingestion-freshness",
            title="Ingestion freshness",
            description="Pct datasets ingested in time",
            metrics=["total", "ingested_in_time"],
            metric_conf={"time_window_sec": 86400},
            dataset_filter={},
        )
        assert req.is_enabled is False
        assert req.mode == "active"
        assert req.metric_type == "ingestion-freshness"

    def test_definition_response(self) -> None:
        """MetricDefinitionResponse has the new field set.

        Spec: spec/API.md §Metric — response carries mode, metric_type, metrics,
              metric_conf, dataset_filter.
        """
        now = datetime.now(tz=UTC)
        resp = MetricDefinitionResponse(
            id="m1",
            mode="active",
            is_enabled=True,
            metric_type="doc-health",
            title="Doc Health",
            description="Documentation coverage",
            metrics=["total", "doc_health"],
            metric_conf={},
            dataset_filter={},
            schedule_tier=None,
            created_at=now,
            updated_at=now,
        )
        assert resp.resp_time is not None
        assert resp.mode == "active"
        assert resp.metric_type == "doc-health"

    def test_list_response(self) -> None:
        """MetricDefinitionListResponse.metrics starts as empty list.

        Spec: spec/API.md §Standard Response Envelope — paginated list defaults.
        """
        resp = MetricDefinitionListResponse()
        assert resp.metrics == []


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

    def test_event_response_wrapper_defaults_false(self) -> None:
        """The derived ``wrapper`` flag defaults to False (own-source event).

        Spec: API.md §Ingestion — GET /sources/{id}/event rows carry a derived
              ``wrapper: bool``; absent/own-source events are False.
        Spec: feature/BACKEND_SCHEMA.md §events — wrapper is computed at read time,
              defaulting False for an event recorded on the source itself.
        """
        now = datetime.now(tz=UTC)
        resp = EventResponse(
            id="e1",
            entity_type="ingestion_source",
            entity_id="src-1",
            event_type="INGESTION.COMPLETE",
            status="success",
            occurred_at=now,
        )
        assert resp.wrapper is False

    def test_event_response_wrapper_true_for_linked_wrapper(self) -> None:
        """``wrapper=True`` round-trips for an event mirrored from a linked wrapper.

        Spec: API.md §Ingestion — GET /sources/{id}/event includes linked-wrapper
              events carrying ``wrapper: true``.
        Spec: feature/BACKEND.md §Sync sweep step 4 — wrapper runs surface on the
              regular source with the derived wrapper flag set.
        """
        now = datetime.now(tz=UTC)
        resp = EventResponse(
            id="e2",
            entity_type="ingestion_source",
            entity_id="wrapper-1",
            event_type="INGESTION.COMPLETE",
            status="success",
            occurred_at=now,
            wrapper=True,
        )
        assert resp.wrapper is True
        assert resp.model_dump()["wrapper"] is True

    def test_event_list_response(self) -> None:
        resp = EventListResponse()
        assert resp.events == []
        assert resp.total_count == 0
