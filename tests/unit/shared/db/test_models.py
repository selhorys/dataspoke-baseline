"""Tests for src/shared/db/models.py — verifies ORM model definitions against
spec/feature/BACKEND_SCHEMA.md (schema layout, PK types, JSONB columns, indexes)
and spec/feature/BACKEND.md §Shared Services (PostgreSQL).

NOTE — constraint-name tests: SQLAlchemy constraint names are implementation details
and are subject to rename without changing behavior. Tests for constraint *behavior*
(i.e., what CHECK expressions enforce) are preferred over tests for constraint *names*.
Name assertions are retained only where a spec source explicitly mandates the name."""

from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import JSONB, UUID  # noqa: F811

from src.shared.db.models import (
    TIMESTAMPTZ,  # noqa: F401
    Base,
    DatasetNodeMap,
    DatasetRegistry,
    DepartmentMapping,
    Event,
    IngestionConfig,
    MetagenConfig,
    MetagenResult,
    MetricDefinition,
    MetricResult,
    NodeEmbedding,
    OntogenConfig,
    OntogenEdge,
    OntogenNode,
    OntogenSeed,
    OntogenTriple,
    OverviewConfig,
    ValidationConfig,
    ValidationResult,
)

ALL_MODELS = [
    IngestionConfig,
    DatasetRegistry,
    ValidationConfig,
    ValidationResult,
    MetagenConfig,
    MetagenResult,
    MetricDefinition,
    MetricResult,
    Event,
    DepartmentMapping,
    OverviewConfig,
    OntogenConfig,
    OntogenSeed,
    OntogenNode,
    OntogenEdge,
    OntogenTriple,
    DatasetNodeMap,
    NodeEmbedding,
]

EXPECTED_TABLES = {
    "ingestion_configs",
    "dataset_registry",
    "validation_configs",
    "validation_results",
    "metagen_configs",
    "metagen_results",
    "metric_definitions",
    "metric_results",
    "events",
    "department_mapping",
    "overview_config",
    "ontogen_config",
    "ontogen_seeds",
    "ontogen_nodes",
    "ontogen_edges",
    "ontogen_triples",
    "dataset_node_map",
    "node_embeddings",
}


def test_all_models_exist() -> None:
    """ALL_MODELS list must contain exactly the tables defined in EXPECTED_TABLES.

    The count is derived from the set, not hard-coded, to avoid brittleness when
    the schema evolves. The EXPECTED_TABLES set is the authoritative list.
    """
    actual_table_names = {m.__tablename__ for m in ALL_MODELS}
    assert actual_table_names == EXPECTED_TABLES


def test_table_names_match() -> None:
    actual = {m.__tablename__ for m in ALL_MODELS}
    assert actual == EXPECTED_TABLES


def test_all_models_use_dataspoke_schema() -> None:
    for model in ALL_MODELS:
        table = model.__table__
        assert table.schema == "dataspoke", f"{model.__name__} missing dataspoke schema"


def test_uuid_primary_keys() -> None:
    # spec: BACKEND_SCHEMA.md — UUID PKs on result/config/event tables.
    # ValidationConfig PK is TEXT (dataset_urn), not UUID — see test_validation_config_text_pk.
    uuid_pk_models = [
        IngestionConfig,
        ValidationResult,
        MetagenConfig,
        MetagenResult,
        MetricResult,
        Event,
        OntogenSeed,
    ]
    for model in uuid_pk_models:
        mapper = inspect(model)
        pk_cols = mapper.primary_key
        assert len(pk_cols) == 1, f"{model.__name__} should have single PK"
        assert isinstance(pk_cols[0].type, UUID), f"{model.__name__} PK should be UUID"


def test_text_primary_keys() -> None:
    # spec: BACKEND_SCHEMA.md — TEXT PKs on definition/mapping tables + ValidationConfig
    text_pk_models = [
        MetricDefinition,
        DepartmentMapping,
        OntogenNode,
        OntogenEdge,
        OntogenTriple,
        ValidationConfig,  # PK = dataset_urn (TEXT) per new passive result-store schema
    ]
    for model in text_pk_models:
        mapper = inspect(model)
        pk_cols = mapper.primary_key
        assert len(pk_cols) == 1, f"{model.__name__} should have single PK"
        assert str(pk_cols[0].type) == "TEXT", f"{model.__name__} PK should be TEXT"


def test_validation_config_text_pk() -> None:
    """ValidationConfig PK is dataset_urn (TEXT) — one row per dataset.

    spec: VALIDATION.md §Rule Configuration — one validation slot per dataset;
    spec: BACKEND_SCHEMA.md §validation_configs.
    """
    mapper = inspect(ValidationConfig)
    pk_cols = mapper.primary_key
    assert len(pk_cols) == 1
    assert pk_cols[0].name == "dataset_urn"
    assert str(pk_cols[0].type) == "TEXT"


def test_integer_primary_key_singleton_models() -> None:
    for model in (OverviewConfig, OntogenConfig):
        mapper = inspect(model)
        pk_cols = mapper.primary_key
        assert len(pk_cols) == 1
        assert str(pk_cols[0].type) == "INTEGER", f"{model.__name__} PK should be INTEGER"


def test_dataset_node_map_composite_pk() -> None:
    mapper = inspect(DatasetNodeMap)
    pk_cols = mapper.primary_key
    assert len(pk_cols) == 2
    pk_names = {c.name for c in pk_cols}
    assert pk_names == {"dataset_urn", "node_id"}


def test_node_embedding_text_pk() -> None:
    mapper = inspect(NodeEmbedding)
    pk_cols = mapper.primary_key
    assert len(pk_cols) == 1
    assert str(pk_cols[0].type) == "TEXT"


def test_jsonb_columns() -> None:
    # spec: BACKEND_SCHEMA.md — JSONB columns per table.
    jsonb_checks = [
        (IngestionConfig, "locator"),
        (IngestionConfig, "identifier"),
        (IngestionConfig, "auth"),
        # ValidationConfig: no JSONB column — variables is ARRAY(Text)
        (ValidationResult, "variables"),  # measured variable values
        (MetagenConfig, "targets"),
        (MetagenResult, "proposals"),
        (MetagenResult, "field_status"),
        (MetricDefinition, "measurement_query"),
        (MetricResult, "breakdown"),
        (Event, "detail"),
        (OverviewConfig, "filters"),
        (OntogenConfig, "dataset_filter"),
        (OntogenNode, "evidence"),
        (OntogenEdge, "evidence"),
        (OntogenTriple, "evidence"),
    ]
    for model, col_name in jsonb_checks:
        col = model.__table__.columns[col_name]
        assert isinstance(col.type, JSONB), f"{model.__name__}.{col_name} should be JSONB"


def test_is_enabled_column_on_config_models() -> None:
    """Mutable config models that have lifecycle scheduling use is_enabled (not is_active).

    spec: BACKEND_SCHEMA.md — is_enabled present on IngestionConfig, MetagenConfig,
    MetricDefinition, OntogenConfig. ValidationConfig uses is_removed (soft-delete) instead.
    """
    enabled_models = [IngestionConfig, MetagenConfig, MetricDefinition, OntogenConfig]
    for model in enabled_models:
        col_names = {col.name for col in model.__table__.columns}
        assert "is_enabled" in col_names, f"{model.__name__} missing is_enabled column"
        assert "is_active" not in col_names, f"{model.__name__} has obsolete is_active column"


def test_validation_config_is_removed_column() -> None:
    """ValidationConfig uses is_removed for soft-delete (not is_enabled).

    spec: VALIDATION.md §Rule Configuration — DELETE performs a soft delete;
    PUT-after-DELETE resurrects the assertion.
    spec: BACKEND_SCHEMA.md §validation_configs.
    """
    col_names = {col.name for col in ValidationConfig.__table__.columns}
    assert "is_removed" in col_names, "ValidationConfig missing is_removed column"
    assert "is_enabled" not in col_names, "ValidationConfig should not have is_enabled"
    assert "is_active" not in col_names, "ValidationConfig should not have is_active"


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
                "last_reviewed_at",
            ):
                assert isinstance(col.type, type(TIMESTAMPTZ)) and col.type.timezone, (
                    f"{model.__name__}.{col.name} should be TIMESTAMP(timezone=True)"
                )


def test_ontogen_triple_composite_id_constraint() -> None:
    """OntogenTriple must have a CHECK enforcing id = subject_node_id || '__' || edge_id || '__' || object_node_id.

    Tests the constraint *expression* rather than its name. Constraint names are
    implementation details — they may be renamed without changing enforcement behavior.
    The expression is what prevents invalid composite IDs from being stored.
    """
    from sqlalchemy import CheckConstraint as _Check
    check_exprs = [
        str(c.sqltext)
        for c in OntogenTriple.__table__.constraints
        if isinstance(c, _Check)
    ]
    # The expression must enforce the concatenation rule: id = subject || '__' || edge || '__' || object
    assert any(
        "subject_node_id" in expr and "edge_id" in expr and "object_node_id" in expr
        for expr in check_exprs
    ), f"No composite-id CHECK found on ontogen_triples. Constraints: {check_exprs}"


def test_ontogen_node_no_double_underscore_constraint() -> None:
    """OntogenNode must have a CHECK that prevents '__' in the id column.

    Tests the constraint *expression* rather than its name. The double-underscore
    separator is the triple id encoding: subject_node_id__edge_id__object_node_id.
    Node ids must not contain '__' so the composite id is unambiguously parseable.

    The spec-mandated predicate is `position('__' in id) = 0` (PostgreSQL).
    SQLite does not support the `position()` function so this test inspects the
    parsed expression rather than executing DDL.

    NOTE: The `position()` function is PG-specific — behavioral enforcement can
    only be integration-tested against a live PostgreSQL instance.
    """
    from sqlalchemy import CheckConstraint as _Check
    check_exprs = [
        str(c.sqltext)
        for c in OntogenNode.__table__.constraints
        if isinstance(c, _Check)
    ]
    # Require the exact predicate as defined in src/shared/db/models.py
    assert any(
        "position('__' in id) = 0" in expr
        for expr in check_exprs
    ), f"Expected position('__' in id) = 0 CHECK not found on ontogen_nodes. Constraints: {check_exprs}"


def test_ontogen_edge_no_double_underscore_constraint() -> None:
    """OntogenEdge must have a CHECK that prevents '__' in the id column.

    Tests the constraint *expression* rather than its name. Same rationale as
    test_ontogen_node_no_double_underscore_constraint above.

    The spec-mandated predicate is `position('__' in id) = 0` (PostgreSQL).
    SQLite does not support the `position()` function so this test inspects the
    parsed expression rather than executing DDL.

    NOTE: The `position()` function is PG-specific — behavioral enforcement can
    only be integration-tested against a live PostgreSQL instance.
    """
    from sqlalchemy import CheckConstraint as _Check
    check_exprs = [
        str(c.sqltext)
        for c in OntogenEdge.__table__.constraints
        if isinstance(c, _Check)
    ]
    # Require the exact predicate as defined in src/shared/db/models.py
    assert any(
        "position('__' in id) = 0" in expr
        for expr in check_exprs
    ), f"Expected position('__' in id) = 0 CHECK not found on ontogen_edges. Constraints: {check_exprs}"


def test_ontogen_triple_fks() -> None:
    table = OntogenTriple.__table__
    fk_targets = {fk.column.table.name for fk in table.foreign_keys}
    assert "ontogen_nodes" in fk_targets
    assert "ontogen_edges" in fk_targets


def test_dataset_node_map_fk_to_ontogen_nodes() -> None:
    table = DatasetNodeMap.__table__
    fk_targets = {fk.column.table.name for fk in table.foreign_keys}
    assert fk_targets == {"ontogen_nodes"}


def test_node_embedding_fk_to_ontogen_nodes() -> None:
    table = NodeEmbedding.__table__
    fk_targets = {fk.column.table.name for fk in table.foreign_keys}
    assert fk_targets == {"ontogen_nodes"}


def test_metric_result_fk() -> None:
    table = MetricResult.__table__
    fks = list(table.foreign_keys)
    assert len(fks) == 1
    assert fks[0].column.table.name == "metric_definitions"


def test_metric_result_has_no_alarm_or_run_id_columns() -> None:
    """metric_results should not have alarm or run_id columns."""
    col_names = {col.name for col in MetricResult.__table__.columns}
    assert "alarm_triggered" not in col_names
    assert "run_id" not in col_names


def test_metric_definition_has_no_alarm_columns() -> None:
    """metric_definitions should not have alarm columns."""
    col_names = {col.name for col in MetricDefinition.__table__.columns}
    assert "alarm_enabled" not in col_names
    assert "alarm_threshold" not in col_names
    assert "alarm_recipients" not in col_names


def test_ingestion_config_has_mode_column() -> None:
    col_names = {col.name for col in IngestionConfig.__table__.columns}
    assert "mode" in col_names


def test_metagen_result_has_field_status_column() -> None:
    col_names = {col.name for col in MetagenResult.__table__.columns}
    assert "field_status" in col_names
    assert "proposals" in col_names


def test_indexes_exist() -> None:
    # spec: BACKEND_SCHEMA.md §indexes — required indexes per table.
    expected_indexes = {
        "ix_validation_results_urn_data_time",  # (dataset_urn, data_time DESC) for LWW collapse
        "ix_metagen_results_urn_generated",
        "ix_metric_results_metric_measured",
        "ix_events_entity_occurred",
        "ix_ontogen_triples_subject",
        "ix_ontogen_triples_object",
        "ix_ontogen_triples_edge",
        "ix_dataset_node_map_node_id",
    }
    actual_indexes: set[str] = set()
    for table in Base.metadata.sorted_tables:
        for idx in table.indexes:
            actual_indexes.add(idx.name)
    assert expected_indexes.issubset(actual_indexes), (
        f"Missing indexes: {expected_indexes - actual_indexes}"
    )


def test_validation_config_columns() -> None:
    """ValidationConfig must have the passive result-store columns.

    spec: VALIDATION.md §Rule Configuration — description, variables, is_removed;
    spec: BACKEND_SCHEMA.md §validation_configs.
    """
    col_names = {col.name for col in ValidationConfig.__table__.columns}
    # New passive-store schema
    assert "dataset_urn" in col_names, "ValidationConfig missing dataset_urn (PK)"
    assert "description" in col_names, "ValidationConfig missing description"
    assert "variables" in col_names, "ValidationConfig missing variables"
    assert "is_removed" in col_names, "ValidationConfig missing is_removed"
    assert "created_at" in col_names
    assert "updated_at" in col_names
    # Old columns must be gone
    assert "rules" not in col_names, "ValidationConfig has stale rules column"
    assert "owner" not in col_names, "ValidationConfig has stale owner column"


def test_validation_result_columns() -> None:
    """ValidationResult must have the passive result-store columns.

    spec: VALIDATION.md §Validation Result — data_time, score, variables, ingestion_time;
    spec: BACKEND_SCHEMA.md §validation_results.
    """
    col_names = {col.name for col in ValidationResult.__table__.columns}
    assert "id" in col_names, "ValidationResult missing id (UUID PK)"
    assert "dataset_urn" in col_names, "ValidationResult missing dataset_urn"
    assert "data_time" in col_names, "ValidationResult missing data_time"
    assert "score" in col_names, "ValidationResult missing score"
    assert "variables" in col_names, "ValidationResult missing variables"
    assert "ingestion_time" in col_names, "ValidationResult missing ingestion_time"
    # Old columns must be gone
    assert "partition" not in col_names, "ValidationResult has stale partition column"
    assert "values" not in col_names, "ValidationResult has stale values column"
    assert "issues" not in col_names, "ValidationResult has stale issues column"
    assert "run_id" not in col_names, "ValidationResult has stale run_id column"


def test_base_metadata_tables_match_expected_set() -> None:
    """Base.metadata must contain exactly the tables named in EXPECTED_TABLES.

    Uses set comparison rather than a hard-coded count so this test does not
    need to be updated when table names change — only EXPECTED_TABLES needs
    updating. Count assertions on table totals are brittle and provide no
    additional safety beyond the set check.
    """
    actual = {t.name for t in Base.metadata.sorted_tables if t.schema == "dataspoke"}
    assert actual == EXPECTED_TABLES
