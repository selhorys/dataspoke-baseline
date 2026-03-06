from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from src.shared.models.dataset import DatasetAttributes, DatasetSummary
from src.shared.models.events import EventRecord
from src.shared.models.ontology import Concept, ConceptRelationship
from src.shared.models.quality import AnomalyResult, QualityIssue, QualityScore

NOW = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)


# ── QualityScore ──────────────────────────────────────────────────────────────


def test_quality_score_serialization() -> None:
    score = QualityScore(
        dataset_urn="urn:li:dataset:(urn:li:dataPlatform:snowflake,db.schema.tbl,PROD)",
        overall_score=87.5,
        dimensions={"completeness": 90.0, "freshness": 85.0},
        computed_at=NOW,
    )
    data = score.model_dump()
    assert data["dataset_urn"].startswith("urn:")
    assert isinstance(data["overall_score"], float)
    assert "dimensions" in data
    assert "computed_at" in data


def test_quality_score_overall_score_is_float() -> None:
    score = QualityScore(
        dataset_urn="urn:x",
        overall_score=100,
        dimensions={},
        computed_at=NOW,
    )
    assert isinstance(score.overall_score, float)


# ── QualityIssue ──────────────────────────────────────────────────────────────


def test_quality_issue_optional_field_path() -> None:
    issue = QualityIssue(issue_type="freshness", severity="warning", detail="stale data")
    assert issue.field_path is None


def test_quality_issue_with_field_path() -> None:
    issue = QualityIssue(
        issue_type="completeness",
        severity="critical",
        detail="null values",
        field_path="column_a",
    )
    assert issue.field_path == "column_a"


# ── AnomalyResult ─────────────────────────────────────────────────────────────


def test_anomaly_result_validation() -> None:
    result = AnomalyResult(
        metric_name="row_count",
        is_anomaly=True,
        expected_value=1000.0,
        actual_value=50.0,
        confidence=0.95,
        detected_at=NOW,
    )
    assert result.is_anomaly is True


def test_anomaly_result_rejects_non_bool_is_anomaly() -> None:
    with pytest.raises(ValidationError):
        AnomalyResult(
            metric_name="row_count",
            is_anomaly={"not": "a bool"},  # type: ignore[arg-type]
            expected_value=1000.0,
            actual_value=50.0,
            confidence=0.95,
            detected_at=NOW,
        )


# ── DatasetSummary ────────────────────────────────────────────────────────────


def test_dataset_summary_defaults() -> None:
    summary = DatasetSummary(urn="urn:x", name="my_table", platform="snowflake")
    assert summary.owners == []
    assert summary.tags == []
    assert summary.description is None


# ── DatasetAttributes ─────────────────────────────────────────────────────────


def test_dataset_attributes_with_quality_score() -> None:
    score = QualityScore(
        dataset_urn="urn:x",
        overall_score=75.0,
        dimensions={"completeness": 75.0},
        computed_at=NOW,
    )
    attrs = DatasetAttributes(
        urn="urn:x",
        column_count=10,
        fields=["col_a", "col_b"],
        quality_score=score,
    )
    data = attrs.model_dump()
    assert data["quality_score"] is not None
    assert data["quality_score"]["overall_score"] == 75.0


def test_dataset_attributes_no_quality_score() -> None:
    attrs = DatasetAttributes(urn="urn:x", column_count=5)
    assert attrs.quality_score is None


# ── Concept ───────────────────────────────────────────────────────────────────


def test_concept_status_default() -> None:
    concept = Concept(
        id="c1",
        name="Revenue",
        description="Business revenue metric",
        created_at=NOW,
        updated_at=NOW,
    )
    assert concept.status == "pending"
    assert concept.version == 1
    assert concept.parent_id is None


# ── ConceptRelationship ───────────────────────────────────────────────────────


def test_concept_relationship_fields() -> None:
    rel = ConceptRelationship(
        id="r1",
        concept_a="c1",
        concept_b="c2",
        relationship_type="related_to",
        confidence_score=0.85,
        created_at=NOW,
    )
    assert rel.relationship_type == "related_to"
    assert rel.confidence_score == 0.85


# ── EventRecord ───────────────────────────────────────────────────────────────


def test_event_record_empty_detail_default() -> None:
    event = EventRecord(
        id="e1",
        entity_type="dataset",
        entity_id="urn:x",
        event_type="ingestion_run",
        status="success",
        occurred_at=NOW,
    )
    assert event.detail == {}


def test_event_record_serialization() -> None:
    event = EventRecord(
        id="e1",
        entity_type="dataset",
        entity_id="urn:x",
        event_type="validation_run",
        status="failure",
        detail={"error": "timeout"},
        occurred_at=NOW,
    )
    data = event.model_dump()
    expected_keys = {
        "id",
        "entity_type",
        "entity_id",
        "event_type",
        "status",
        "detail",
        "occurred_at",
    }
    assert expected_keys.issubset(data.keys())
    assert data["detail"] == {"error": "timeout"}
