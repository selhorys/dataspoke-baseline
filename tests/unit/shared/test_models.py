"""Tests for src/shared/models/ — verifies shared Pydantic domain models (QualityScore,
QualityIssue, DatasetSummary, DatasetAttributes, EventRecord) against spec/feature/BACKEND.md
§Shared Services (Domain Models row): internal domain objects, not API schemas.
Also covers URN round-trip and InvalidDatasetUrnError rejection per spec/API.md L586
and spec/DATAHUB_INTEGRATION.md §URN Construction."""

from datetime import UTC, datetime

import pytest

from src.shared.exceptions import InvalidDatasetUrnError
from src.shared.models.dataset import DatasetAttributes, DatasetSummary
from src.shared.models.events import EventRecord
from src.shared.models.quality import QualityIssue, QualityScore

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


# ── URN Construction (spec/DATAHUB_INTEGRATION.md §URN Construction) ──────────


@pytest.mark.parametrize(
    "platform,name,env",
    [
        ("postgres", "example_db.catalog.title_master", "DEV"),
        ("snowflake", "db.schema.orders", "PROD"),
        ("bigquery", "project.dataset.table", "DEV"),
        ("oracle", "catalog.title_master", "PROD"),
    ],
)
def test_make_dataset_urn_round_trip(platform: str, name: str, env: str) -> None:
    """make_dataset_urn produces a well-formed URN that round-trips through DatasetUrn parser.

    Verifies spec/DATAHUB_INTEGRATION.md §URN Construction: 'Always use the builder
    function — never construct URN strings manually.' The built URN must conform to
    the urn:li:dataset:(urn:li:dataPlatform:<platform>,<name>,<env>) schema so it
    is accepted by DataHub and by DataSpoke's own URN validation (spec/API.md L586).

    Genuine round-trip: parse the produced URN back to (platform, name, env) via
    DatasetUrn and verify equality with the original inputs.
    """
    import re

    from datahub.emitter.mce_builder import make_dataset_urn
    from datahub.utilities.urns.dataset_urn import DatasetUrn

    urn = make_dataset_urn(platform=platform, name=name, env=env)
    # Must start with the dataset URN prefix
    assert urn.startswith("urn:li:dataset:(")
    assert urn.endswith(")")
    # Platform must be embedded in the URN
    assert f"urn:li:dataPlatform:{platform}" in urn
    # Environment must be present
    assert env in urn
    # Must be accepted by the DataSpoke ontogen URN validator (regex: ^urn:li:dataset:\(.+\)$)
    _DATASET_URN_RE = re.compile(r"^urn:li:dataset:\(.+\)$")
    assert _DATASET_URN_RE.match(urn), f"make_dataset_urn output {urn!r} fails DataSpoke URN regex"
    # Genuine round-trip: parse the URN and verify platform/name/env identity
    parsed = DatasetUrn.from_string(urn)
    assert parsed.env == env, f"Parsed env {parsed.env!r} != original {env!r}"
    assert parsed.platform == f"urn:li:dataPlatform:{platform}", (
        f"Parsed platform {parsed.platform!r} != urn:li:dataPlatform:{platform!r}"
    )
    assert parsed.name == name, f"Parsed name {parsed.name!r} != original {name!r}"


@pytest.mark.parametrize(
    "platform,name,env,expected_encoded_in_urn,expected_parsed_name",
    [
        # Commas are percent-encoded in the URN (%2C). The SDK does NOT decode on parse —
        # DatasetUrn.name returns the percent-encoded form for comma-containing names.
        # Spec/DATAHUB_INTEGRATION.md §URN Construction mandates builder usage for safety.
        (
            "postgres",
            "db.schema.table,with,commas",
            "DEV",
            "db.schema.table%2Cwith%2Ccommas",
            "db.schema.table%2Cwith%2Ccommas",
        ),
        # Unicode names pass through unencoded in both the URN and the parsed form.
        (
            "postgres",
            "테이블",
            "DEV",
            "테이블",
            "테이블",
        ),
        # Parentheses are percent-encoded (%28/%29). DatasetUrn.name returns encoded form.
        (
            "postgres",
            "table(with)parens",
            "DEV",
            "table%28with%29parens",
            "table%28with%29parens",
        ),
    ],
)
def test_make_dataset_urn_special_characters(
    platform: str,
    name: str,
    env: str,
    expected_encoded_in_urn: str,
    expected_parsed_name: str,
) -> None:
    """make_dataset_urn encodes special characters in dataset names.

    Verifies spec/DATAHUB_INTEGRATION.md §URN Construction: names containing commas,
    parentheses, or unicode produce valid URNs. Percent-encoded characters (commas,
    parens) remain encoded in DatasetUrn.name — the SDK encodes on build but does NOT
    decode on parse. Unicode names are preserved as-is through the round-trip.

    Callers storing encoded names must handle the percent-encoded form consistently
    when looking up datasets by name.
    """
    from datahub.emitter.mce_builder import make_dataset_urn
    from datahub.utilities.urns.dataset_urn import DatasetUrn

    urn = make_dataset_urn(platform=platform, name=name, env=env)
    assert expected_encoded_in_urn in urn, (
        f"Expected encoded segment {expected_encoded_in_urn!r} not found in URN {urn!r}"
    )
    # Round-trip: DatasetUrn.name returns the encoded form for percent-encoded chars
    parsed = DatasetUrn.from_string(urn)
    assert parsed.name == expected_parsed_name, (
        f"Parsed name {parsed.name!r} != expected {expected_parsed_name!r}"
    )
    assert parsed.env == env


def test_make_dataset_urn_empty_env_rejected() -> None:
    """make_dataset_urn rejects empty env strings — spec/DATAHUB_INTEGRATION.md §URN Construction.

    DataHub URN requires a non-empty env component. The SDK raises an error if env=''.
    This is documented behaviour, not a bug — callers must supply a valid env.
    """
    from datahub.emitter.mce_builder import make_dataset_urn

    with pytest.raises(Exception):
        make_dataset_urn(platform="postgres", name="table", env="")


@pytest.mark.parametrize(
    "bad_urn",
    [
        "not-a-urn",
        "urn:li:dataset:",
        "urn:li:dataset:()",
        "urn:li:dataset:without-parens,platform,env",
        "",
        "urn:li:dataset",
        "urn:li:chart:(urn:li:dataPlatform:postgres,db.table,DEV)",
    ],
)
def test_invalid_dataset_urn_rejected_by_ontogen_validator(bad_urn: str) -> None:
    """Malformed URNs are rejected by the shared check_dataset_urn_format function.

    Verifies spec/API.md L586: 'INVALID_DATASET_URN — A dataset_filter.dataset_urns
    entry is not a well-formed urn:li:dataset:(…) URN.' The shared validator at
    src/api/schemas/_dataset_filter.py rejects strings that do not match
    ^urn:li:dataset:\\(.+\\)$ and raises InvalidDatasetUrnError.
    """
    from src.api.schemas._dataset_filter import check_dataset_urn_format

    with pytest.raises(InvalidDatasetUrnError):
        check_dataset_urn_format({"dataset_urns": [bad_urn]})


def test_invalid_dataset_urn_error_code() -> None:
    """InvalidDatasetUrnError.error_code must be INVALID_DATASET_URN per spec/API.md L586."""
    exc = InvalidDatasetUrnError("bad-urn-string")
    assert exc.error_code == "INVALID_DATASET_URN"
    assert "bad-urn-string" in str(exc)
    assert isinstance(exc, InvalidDatasetUrnError)
