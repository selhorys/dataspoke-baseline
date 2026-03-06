"""Unit tests for SQLAlchemy ORM models — metadata introspection only, no DB needed."""

from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import JSONB, UUID  # noqa: F811

from src.shared.db.models import (
    TIMESTAMPTZ,  # noqa: F401
    Base,
    ConceptCategory,
    ConceptRelationship,
    DatasetConceptMap,
    DepartmentMapping,
    Event,
    GenerationConfig,
    GenerationResult,
    IngestionConfig,
    MetricDefinition,
    MetricIssue,
    MetricResult,
    OverviewConfig,
    ValidationConfig,
    ValidationResult,
)

ALL_MODELS = [
    IngestionConfig,
    ValidationConfig,
    ValidationResult,
    GenerationConfig,
    GenerationResult,
    ConceptCategory,
    DatasetConceptMap,
    ConceptRelationship,
    MetricDefinition,
    MetricResult,
    MetricIssue,
    Event,
    DepartmentMapping,
    OverviewConfig,
]

EXPECTED_TABLES = {
    "ingestion_configs",
    "validation_configs",
    "validation_results",
    "generation_configs",
    "generation_results",
    "concept_categories",
    "dataset_concept_map",
    "concept_relationships",
    "metric_definitions",
    "metric_results",
    "metric_issues",
    "events",
    "department_mapping",
    "overview_config",
}


def test_all_14_models_exist() -> None:
    assert len(ALL_MODELS) == 14


def test_table_names_match() -> None:
    actual = {m.__tablename__ for m in ALL_MODELS}
    assert actual == EXPECTED_TABLES


def test_all_models_use_dataspoke_schema() -> None:
    for model in ALL_MODELS:
        table = model.__table__
        assert table.schema == "dataspoke", f"{model.__name__} missing dataspoke schema"


def test_uuid_primary_keys() -> None:
    uuid_pk_models = [
        IngestionConfig,
        ValidationConfig,
        ValidationResult,
        GenerationConfig,
        GenerationResult,
        ConceptCategory,
        ConceptRelationship,
        MetricResult,
        MetricIssue,
        Event,
    ]
    for model in uuid_pk_models:
        mapper = inspect(model)
        pk_cols = mapper.primary_key
        assert len(pk_cols) == 1, f"{model.__name__} should have single PK"
        assert isinstance(pk_cols[0].type, UUID), f"{model.__name__} PK should be UUID"


def test_text_primary_keys() -> None:
    mapper = inspect(MetricDefinition)
    pk_cols = mapper.primary_key
    assert len(pk_cols) == 1
    assert str(pk_cols[0].type) == "TEXT"

    mapper = inspect(DepartmentMapping)
    pk_cols = mapper.primary_key
    assert len(pk_cols) == 1
    assert str(pk_cols[0].type) == "TEXT"


def test_integer_primary_key_overview_config() -> None:
    mapper = inspect(OverviewConfig)
    pk_cols = mapper.primary_key
    assert len(pk_cols) == 1
    assert str(pk_cols[0].type) == "INTEGER"


def test_dataset_concept_map_composite_pk() -> None:
    mapper = inspect(DatasetConceptMap)
    pk_cols = mapper.primary_key
    assert len(pk_cols) == 2
    pk_names = {c.name for c in pk_cols}
    assert pk_names == {"dataset_urn", "concept_id"}


def test_jsonb_columns() -> None:
    jsonb_checks = [
        (IngestionConfig, "sources"),
        (ValidationConfig, "rules"),
        (ValidationConfig, "sla_target"),
        (ValidationResult, "dimensions"),
        (GenerationConfig, "target_fields"),
        (GenerationResult, "proposals"),
        (MetricDefinition, "measurement_query"),
        (MetricResult, "breakdown"),
        (Event, "detail"),
        (OverviewConfig, "filters"),
    ]
    for model, col_name in jsonb_checks:
        col = model.__table__.columns[col_name]
        assert isinstance(col.type, JSONB), f"{model.__name__}.{col_name} should be JSONB"


def test_timestamptz_columns() -> None:
    for model in ALL_MODELS:
        table = model.__table__
        for col in table.columns:
            if col.name in (
                "created_at",
                "updated_at",
                "measured_at",
                "occurred_at",
                "generated_at",
                "applied_at",
                "resolved_at",
                "due_date",
            ):
                assert isinstance(col.type, type(TIMESTAMPTZ)) and col.type.timezone, (
                    f"{model.__name__}.{col.name} should be TIMESTAMP(timezone=True)"
                )


def test_concept_category_self_referencing_fk() -> None:
    table = ConceptCategory.__table__
    fks = list(table.foreign_keys)
    assert len(fks) == 1
    assert fks[0].column.table.name == "concept_categories"


def test_concept_relationship_fks() -> None:
    table = ConceptRelationship.__table__
    fk_targets = {fk.column.table.name for fk in table.foreign_keys}
    assert fk_targets == {"concept_categories"}
    assert len(list(table.foreign_keys)) == 2


def test_dataset_concept_map_fk() -> None:
    table = DatasetConceptMap.__table__
    fks = list(table.foreign_keys)
    assert len(fks) == 1
    assert fks[0].column.table.name == "concept_categories"


def test_metric_result_fk() -> None:
    table = MetricResult.__table__
    fks = list(table.foreign_keys)
    assert len(fks) == 1
    assert fks[0].column.table.name == "metric_definitions"


def test_metric_issue_fk() -> None:
    table = MetricIssue.__table__
    fks = list(table.foreign_keys)
    assert len(fks) == 1
    assert fks[0].column.table.name == "metric_definitions"


def test_indexes_exist() -> None:
    expected_indexes = {
        "ix_validation_results_urn_measured",
        "ix_generation_results_urn_generated",
        "ix_metric_results_metric_measured",
        "ix_events_entity_occurred",
        "ix_metric_issues_status_priority",
        "ix_metric_issues_urn_status",
        "ix_metric_issues_metric_created",
        "ix_dataset_concept_map_concept",
        "ix_concept_categories_parent",
    }
    actual_indexes: set[str] = set()
    for table in Base.metadata.sorted_tables:
        for idx in table.indexes:
            actual_indexes.add(idx.name)
    assert expected_indexes.issubset(actual_indexes), (
        f"Missing indexes: {expected_indexes - actual_indexes}"
    )


def test_base_metadata_has_14_tables() -> None:
    tables_in_schema = [t for t in Base.metadata.sorted_tables if t.schema == "dataspoke"]
    assert len(tables_in_schema) == 14
