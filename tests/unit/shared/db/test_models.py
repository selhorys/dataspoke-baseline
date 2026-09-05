"""Tests for src/shared/db/models.py — verifies ORM model definitions against
spec/feature/BACKEND_SCHEMA.md (schema layout, PK types, JSONB columns, indexes)
and spec/feature/BACKEND.md §Shared Services (PostgreSQL).

NOTE — constraint-name tests: SQLAlchemy constraint names are implementation details
and are subject to rename without changing behavior. Tests for constraint *behavior*
(i.e., what CHECK expressions enforce) are preferred over tests for constraint *names*.
Name assertions are retained only where a spec source explicitly mandates the name."""

from sqlalchemy import ARRAY, TIMESTAMP, Boolean, Text, inspect
from sqlalchemy.dialects.postgresql import JSONB, UUID  # noqa: F811

from src.shared.db.models import (
    TIMESTAMPTZ,  # noqa: F401
    ApiToken,
    Base,
    DatasetNodeMap,
    DatasetRegistry,
    EdgeEmbedding,
    Event,
    IngestionSource,
    IngestionSourceDataset,
    MetagenBoundary,
    MetagenCandidate,
    MetagenCandidateEmbedding,
    MetagenConfig,
    MetagenItem,
    MetricDatasetResult,
    MetricDefinition,
    MetricResult,
    NodeEmbedding,
    OntogenConfig,
    OntogenEdge,
    OntogenNode,
    OntogenSeed,
    OntogenTriple,
    PasswordResetToken,
    PeripheralConfig,
    PeripheralHealth,
    RuntimeConfig,
    TripleEmbedding,
    User,
    ValidationConfig,
    ValidationResult,
)

ALL_MODELS = [
    User,
    ApiToken,
    PasswordResetToken,
    IngestionSource,
    IngestionSourceDataset,
    DatasetRegistry,
    ValidationConfig,
    ValidationResult,
    MetagenConfig,
    MetagenBoundary,
    MetagenItem,
    MetagenCandidate,
    MetagenCandidateEmbedding,
    MetricDefinition,
    MetricResult,
    MetricDatasetResult,
    Event,
    OntogenConfig,
    RuntimeConfig,
    PeripheralConfig,
    PeripheralHealth,
    OntogenSeed,
    OntogenNode,
    OntogenEdge,
    OntogenTriple,
    DatasetNodeMap,
    NodeEmbedding,
    EdgeEmbedding,
    TripleEmbedding,
]

EXPECTED_TABLES = {
    "users",
    "api_tokens",
    "password_reset_tokens",
    "ingestion_source",
    "ingestion_source_dataset",
    "dataset_registry",
    "validation_configs",
    "validation_results",
    "metagen_config",
    "metagen_boundary",
    "metagen_items",
    "metagen_candidates",
    "metagen_candidate_embeddings",
    "metric_definitions",
    "metric_results",
    "metric_dataset_results",
    "events",
    "ontogen_config",
    "runtime_config",
    "peripheral_config",
    "peripheral_health",
    "ontogen_seeds",
    "ontogen_nodes",
    "ontogen_edges",
    "ontogen_triples",
    "dataset_node_map",
    "node_embeddings",
    "edge_embeddings",
    "triple_embeddings",
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
    # MetagenConfig PK is INTEGER (singleton); MetagenCandidate PK is UUID.
    uuid_pk_models = [
        IngestionSource,
        ValidationResult,
        MetagenCandidate,
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
    for model in (OntogenConfig,):
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
        (IngestionSource, "recipe"),
        # ValidationConfig: no JSONB column — variables is ARRAY(Text)
        (ValidationResult, "variables"),  # measured variable values
        (MetagenCandidate, "evidence"),
        (MetricDefinition, "metrics"),
        (MetricDefinition, "metric_conf"),
        (MetricResult, "values"),
        (MetricResult, "breakdown"),
        (MetricDatasetResult, "detail"),
        (Event, "detail"),
    ]
    for model, col_name in jsonb_checks:
        col = model.__table__.columns[col_name]
        assert isinstance(col.type, JSONB), f"{model.__name__}.{col_name} should be JSONB"


def test_dataset_filter_columns_are_text_on_every_carrier() -> None:
    """`dataset_filter` is a SQL WHERE-clause string, not a structured object.

    spec: BACKEND_SCHEMA.md §metagen_config / §ontogen_config / §metric_definitions —
    '`dataset_filter` | `TEXT` | Scope filter — a SQL `WHERE` clause over
    `dataset_registry` […]; `''` = all registered datasets'. All three carriers
    share one grammar, so all three share one column type.
    """
    for model in (MetagenConfig, OntogenConfig, MetricDefinition):
        col = model.__table__.columns["dataset_filter"]
        assert isinstance(col.type, Text), f"{model.__name__}.dataset_filter should be TEXT"
        assert col.nullable is False, f"{model.__name__}.dataset_filter should be NOT NULL"


def test_dataset_registry_carries_the_filter_attribute_columns() -> None:
    """The registry mirrors the attributes `dataset_filter` is evaluated against.

    spec: BACKEND_SCHEMA.md §dataset_registry — `origin` TEXT NULL, `platform_urn`
    TEXT NULL, `tag_urns` TEXT[] NOT NULL, `glossary_term_urns` TEXT[] NOT NULL,
    `is_primary` BOOLEAN NOT NULL DEFAULT `true`, `attrs_synced_at` TIMESTAMPTZ NULL.
    """
    columns = DatasetRegistry.__table__.columns

    for name in ("origin", "platform_urn"):
        assert isinstance(columns[name].type, Text), f"{name} should be TEXT"
        assert columns[name].nullable is True, f"{name} should be nullable"

    for name in ("tag_urns", "glossary_term_urns"):
        col_type = columns[name].type
        assert isinstance(col_type, ARRAY), f"{name} should be an array column"
        assert isinstance(col_type.item_type, Text), f"{name} should be TEXT[]"
        assert columns[name].nullable is False, f"{name} should be NOT NULL"

    # `is_primary` — "`BOOLEAN` NOT NULL DEFAULT `true` […] Not null: absent sibling
    # information means primary, so a never-swept row is counted once rather than
    # dropped." A nullable column would make `is_primary = true` skip every row the
    # sweep has not reached, silently narrowing UC3/UC4/UC5 scope.
    assert isinstance(columns["is_primary"].type, Boolean), "is_primary should be BOOLEAN"
    assert columns["is_primary"].nullable is False, "is_primary should be NOT NULL"
    assert columns["is_primary"].server_default is not None, (
        "is_primary needs a server-side DEFAULT true, or a row inserted by any writer "
        "that does not name the column violates NOT NULL. "
        "spec: BACKEND_SCHEMA.md §dataset_registry."
    )
    assert "true" in str(columns["is_primary"].server_default.arg).lower(), (
        "the default must be true — the 'no sibling information ⇒ primary' rule. "
        f"got {columns['is_primary'].server_default.arg!r}. "
        "spec: BACKEND_SCHEMA.md §dataset_registry."
    )
    # The Python-side default is the one that actually fires: both row-creating paths
    # in `src/shared/db/registry.py` construct `DatasetRegistry(...)` through the ORM
    # without naming this column, so SQLAlchemy emits the client default and the
    # server default never runs. A python default of `False` would write every
    # lazily- and bulk-registered dataset as non-primary and drop it out of every
    # `is_primary = true` filter — exactly the failure the NOT NULL DEFAULT true rule
    # exists to prevent.
    assert columns["is_primary"].default is not None, (
        "is_primary needs a Python-side default too — ORM inserts that omit the "
        "column would otherwise reach the DB with no value. "
        "spec: BACKEND_SCHEMA.md §dataset_registry."
    )
    assert columns["is_primary"].default.arg is True, (
        "the Python-side default must be True — 'absent sibling information means "
        "primary, so a never-swept row is counted once rather than dropped'. got "
        f"{columns['is_primary'].default.arg!r}. "
        "spec: BACKEND_SCHEMA.md §dataset_registry."
    )

    assert isinstance(columns["attrs_synced_at"].type, TIMESTAMP)
    assert columns["attrs_synced_at"].type.timezone is True
    assert columns["attrs_synced_at"].nullable is True


def test_dataset_registry_indexes_the_selective_side_of_is_primary() -> None:
    """The `false` side of `is_primary` carries a partial index.

    spec: BACKEND_SCHEMA.md §Indexes — "`dataset_registry` |
        `ix_dataset_registry_not_primary`: `(is_primary) WHERE NOT is_primary` |
        `is_primary = false` predicates in `dataset_filter`; partial because the
        column defaults to `true` registry-wide".
    """
    index = next(
        (
            idx
            for idx in DatasetRegistry.__table__.indexes
            if idx.name == "ix_dataset_registry_not_primary"
        ),
        None,
    )
    assert index is not None, (
        "ix_dataset_registry_not_primary is missing; got "
        f"{sorted(i.name for i in DatasetRegistry.__table__.indexes)}. "
        "spec: BACKEND_SCHEMA.md §Indexes."
    )
    assert [col.name for col in index.columns] == ["is_primary"]
    where = index.dialect_options["postgresql"].get("where")
    assert where is not None, (
        "the index must be partial — a full btree on a column that is true "
        "registry-wide is not selective enough for the planner to use. "
        "spec: BACKEND_SCHEMA.md §Indexes."
    )
    assert "not is_primary" in str(where).lower(), (
        f"the index predicate must be `NOT is_primary`; got {where!r}. "
        "spec: BACKEND_SCHEMA.md §Indexes."
    )


def test_metric_dataset_results_is_keyed_by_metric_and_dataset() -> None:
    """The verdict store holds the latest verdict per (metric, dataset).

    spec: BACKEND_SCHEMA.md §metric_dataset_results — '**PK**: `(metric_id,
    dataset_urn)`' and the column table (`met` BOOLEAN, `evidence_at` TIMESTAMPTZ
    NULL, `detail` JSONB, `measured_at` TIMESTAMPTZ).
    """
    table = MetricDatasetResult.__table__
    assert {col.name for col in table.primary_key.columns} == {"metric_id", "dataset_urn"}

    assert isinstance(table.columns["met"].type, Boolean)
    assert table.columns["evidence_at"].nullable is True
    assert isinstance(table.columns["measured_at"].type, TIMESTAMP)


def test_metric_dataset_results_cascades_from_its_metric() -> None:
    """spec: BACKEND_SCHEMA.md §metric_dataset_results — '`metric_id` | `TEXT` FK →
    `metric_definitions(id)` ON DELETE CASCADE'."""
    fks = list(MetricDatasetResult.__table__.columns["metric_id"].foreign_keys)
    assert len(fks) == 1
    assert fks[0].column.table.name == "metric_definitions"
    assert fks[0].ondelete == "CASCADE"


def test_is_enabled_column_on_config_models() -> None:
    """Mutable config models that have lifecycle scheduling use is_enabled (not is_active).

    spec: BACKEND_SCHEMA.md — is_enabled present on MetagenConfig, MetagenBoundary,
    MetricDefinition, OntogenConfig, OntogenSeed. IngestionSource uses status column
    instead. ValidationConfig has no lifecycle flag (hard-delete, no soft-delete).
    """
    enabled_models = [
        MetagenConfig,
        MetagenBoundary,
        MetricDefinition,
        OntogenConfig,
        OntogenSeed,
    ]
    for model in enabled_models:
        col_names = {col.name for col in model.__table__.columns}
        assert "is_enabled" in col_names, f"{model.__name__} missing is_enabled column"
        assert "is_active" not in col_names, f"{model.__name__} has obsolete is_active column"


def test_validation_config_has_no_lifecycle_flag() -> None:
    """ValidationConfig has no is_removed/is_enabled/is_active lifecycle flag.

    DELETE is a hard delete + cascade — there is no soft-delete (freeze) state to
    track on the row.

    spec: VALIDATION.md §Rule Configuration — DELETE is a hard delete.
    spec: BACKEND_SCHEMA.md §validation_configs.
    """
    col_names = {col.name for col in ValidationConfig.__table__.columns}
    assert "is_removed" not in col_names, "ValidationConfig should not have is_removed"
    assert "is_enabled" not in col_names, "ValidationConfig should not have is_enabled"
    assert "is_active" not in col_names, "ValidationConfig should not have is_active"


def test_ontogen_seed_uses_is_enabled_not_status() -> None:
    """OntogenSeed uses an is_enabled lifecycle flag, not a status column.

    A seed ships disabled and is enabled/disabled to join or leave the inference
    pipeline; there is no active/retired status.

    spec: BACKEND_SCHEMA.md §ontogen_seeds — is_enabled (default false).
    spec: USE_CASE_en.md §UC3 — seeds created disabled, enabled via attr/enabled.
    """
    col_names = {col.name for col in OntogenSeed.__table__.columns}
    assert "is_enabled" in col_names, "OntogenSeed missing is_enabled column"
    assert "status" not in col_names, "OntogenSeed must not carry a status column"
    assert "body_md" in col_names, "OntogenSeed missing body_md column"


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
    """OntogenTriple must have a CHECK enforcing the composite id pattern.

    id = subject_node_id || '__' || edge_id || '__' || object_node_id.

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
    # The expression must enforce: id = subject || '__' || edge || '__' || object
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
    expected_predicate = "position('__' in id) = 0"
    assert any(expected_predicate in expr for expr in check_exprs), (
        f"Expected {expected_predicate!r} CHECK not found on ontogen_nodes. "
        f"Constraints: {check_exprs}"
    )


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
    expected_predicate = "position('__' in id) = 0"
    assert any(expected_predicate in expr for expr in check_exprs), (
        f"Expected {expected_predicate!r} CHECK not found on ontogen_edges. "
        f"Constraints: {check_exprs}"
    )


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


def test_ingestion_source_has_required_columns() -> None:
    """IngestionSource has all columns specified in BACKEND_SCHEMA.md §ingestion_source.

    spec: BACKEND_SCHEMA.md §ingestion_source — mode, name, platform, recipe,
          schedule, schedule_tier, datahub_source_urn, status columns.
    """
    col_names = {col.name for col in IngestionSource.__table__.columns}
    for expected in (
        "mode", "name", "platform", "recipe", "schedule", "schedule_tier",
        "datahub_source_urn", "status", "created_at", "updated_at",
    ):
        assert expected in col_names, (
            f"IngestionSource missing column '{expected}'. "
            "spec: BACKEND_SCHEMA.md §ingestion_source."
        )


def test_ingestion_source_dataset_has_required_columns() -> None:
    """IngestionSourceDataset has all columns per BACKEND_SCHEMA.md §ingestion_source_dataset.

    spec: BACKEND_SCHEMA.md §ingestion_source_dataset — source_id, dataset_urn, derivation,
          first_seen_at, last_seen_at.
    """
    col_names = {col.name for col in IngestionSourceDataset.__table__.columns}
    for expected in ("source_id", "dataset_urn", "derivation", "first_seen_at", "last_seen_at"):
        assert expected in col_names, (
            f"IngestionSourceDataset missing column '{expected}'. "
            "spec: BACKEND_SCHEMA.md §ingestion_source_dataset."
        )


def test_ingestion_source_dataset_fk_to_ingestion_source() -> None:
    """IngestionSourceDataset.source_id is a FK → ingestion_source(id) ON DELETE CASCADE.

    spec: BACKEND_SCHEMA.md §ingestion_source_dataset.
    """
    table = IngestionSourceDataset.__table__
    fk_targets = {fk.column.table.name for fk in table.foreign_keys}
    assert "ingestion_source" in fk_targets


def test_metagen_candidate_columns() -> None:
    """MetagenCandidate must have the columns added in the reshape."""
    col_names = {col.name for col in MetagenCandidate.__table__.columns}
    assert "candidate_id" in col_names
    assert "dataset_urn" in col_names
    assert "item_id" in col_names
    assert "run_id" in col_names
    assert "value" in col_names
    assert "confidence_score" in col_names
    assert "status" in col_names
    assert "evidence" in col_names


def test_indexes_exist() -> None:
    # spec: BACKEND_SCHEMA.md §indexes — required indexes per table.
    expected_indexes = {
        "ix_validation_results_urn_data_time",  # (dataset_urn, data_time DESC) for LWW collapse
        "ix_metagen_candidates_item_status_created",
        "ix_metagen_candidates_run_id",
        "ix_metagen_candidates_one_approved",
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

    spec: VALIDATION.md §Rule Configuration — description, variables;
    spec: BACKEND_SCHEMA.md §validation_configs.
    """
    col_names = {col.name for col in ValidationConfig.__table__.columns}
    # New passive-store schema
    assert "dataset_urn" in col_names, "ValidationConfig missing dataset_urn (PK)"
    assert "description" in col_names, "ValidationConfig missing description"
    assert "variables" in col_names, "ValidationConfig missing variables"
    assert "created_at" in col_names
    assert "updated_at" in col_names
    # Old columns must be gone
    assert "rules" not in col_names, "ValidationConfig has stale rules column"
    assert "owner" not in col_names, "ValidationConfig has stale owner column"
    # Soft-delete is gone: hard-delete + cascade replaces the is_removed freeze.
    assert "is_removed" not in col_names, (
        "ValidationConfig must not carry is_removed — DELETE is a hard delete + cascade. "
        "spec: BACKEND_SCHEMA.md §validation_configs"
    )


def test_validation_result_columns() -> None:
    """ValidationResult must have the passive result-store columns.

    spec: VALIDATION.md §Validation Result — data_time, score, variables;
    spec: BACKEND_SCHEMA.md §validation_results — ingestion_time audit column.
    """
    col_names = {col.name for col in ValidationResult.__table__.columns}
    assert "id" in col_names, "ValidationResult missing id (UUID PK)"
    assert "dataset_urn" in col_names, "ValidationResult missing dataset_urn"
    assert "data_time" in col_names, "ValidationResult missing data_time"
    assert "score" in col_names, "ValidationResult missing score"
    assert "variables" in col_names, "ValidationResult missing variables"
    assert "score_note" in col_names, "ValidationResult missing score_note"
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


def test_department_mapping_table_absent() -> None:
    """department_mapping must not exist in Base.metadata.

    spec: BACKEND_SCHEMA.md — dead table removed; DepartmentMapping model deleted.
    """
    all_table_names = {t.name for t in Base.metadata.sorted_tables if t.schema == "dataspoke"}
    assert "department_mapping" not in all_table_names, (
        "department_mapping table should be removed from the schema"
    )


# ── peripheral_health ────────────────────────────────────────────────────────


def test_peripheral_health_columns_and_types() -> None:
    """peripheral_health carries name/status/last_error/last_ok_at/updated_at.

    spec: BACKEND_SCHEMA.md §peripheral_health — the five-column table written by
    "the processes that exercise that transport" and read back by
    GET /admin/peripherals/datahub.
    """
    cols = PeripheralHealth.__table__.columns
    assert {c.name for c in cols} == {
        "name",
        "status",
        "last_error",
        "last_ok_at",
        "updated_at",
    }
    assert str(cols["name"].type) == "VARCHAR(32)"
    assert str(cols["status"].type) == "VARCHAR(16)"
    assert str(cols["last_error"].type) == "TEXT"
    assert cols["last_error"].nullable, "last_error is NULL when the peripheral never failed"
    assert cols["last_ok_at"].nullable, "last_ok_at is NULL when it never succeeded"


def test_peripheral_health_name_is_the_primary_key() -> None:
    """One row per transport — reports upsert rather than accumulate.

    spec: BACKEND_SCHEMA.md §peripheral_health — "A row is upserted on report, so
    the table never grows past the transport set and carries no history."
    """
    pk_cols = inspect(PeripheralHealth).primary_key
    assert len(pk_cols) == 1
    assert pk_cols[0].name == "name"


def test_peripheral_health_has_no_foreign_key_to_peripheral_config() -> None:
    """The two tables are independent — a missing config row must not block a report.

    spec: BACKEND_SCHEMA.md §peripheral_health — "no foreign key" to
    ``peripheral_config``; a constraint "would make the health upsert fail precisely
    when the peripheral_config row is missing", suppressing the signal the table exists
    to carry.
    """
    assert PeripheralHealth.__table__.foreign_keys == set(), (
        "peripheral_health must declare no foreign keys; found "
        f"{PeripheralHealth.__table__.foreign_keys!r}"
    )


def test_peripheral_health_check_constraints_pin_both_domains() -> None:
    """CHECKs restrict ``name`` to the transport set and ``status`` to the three states.

    spec: BACKEND_SCHEMA.md §peripheral_health — ``name`` CHECK ∈ datahub,
    datahub-api, langfuse, smtp (one row per transport: ``datahub`` is the Kafka
    event stream, ``datahub-api`` the GMS metadata API); ``status`` CHECK ∈
    unknown, ok, error.
    """
    from sqlalchemy import CheckConstraint

    checks = {
        str(c.sqltext)
        for c in PeripheralHealth.__table__.constraints
        if isinstance(c, CheckConstraint)
    }
    joined = " ".join(checks)
    for value in ("'datahub'", "'datahub-api'", "'langfuse'", "'smtp'"):
        assert value in joined, f"name CHECK must admit {value!r}; constraints: {checks!r}"
    for value in ("unknown", "ok", "error"):
        assert value in joined, f"status CHECK must admit {value!r}; constraints: {checks!r}"


def test_peripheral_health_status_defaults_to_unknown() -> None:
    """A freshly inserted row reads ``unknown`` until a reporter writes.

    spec: BACKEND_SCHEMA.md §peripheral_health — "``unknown`` until a reporter writes";
    "Absence of a row and status='unknown' mean the same thing to readers".
    """
    status_col = PeripheralHealth.__table__.columns["status"]
    assert status_col.server_default is not None, (
        "status needs a server-side default so a direct-SQL insert still reads 'unknown'"
    )
    assert "unknown" in str(status_col.server_default.arg)
