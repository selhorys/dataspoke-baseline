"""Unit tests for the doc-health measurer.

Spec sources:
  spec/USE_CASE_en.md §UC5 §Built-in active metric types:
    - Registered under 'doc-health'.
    - Emits {'total': float, 'doc_health': float}.
    - A dataset scores 1.0 iff it has a non-empty table description AND every
      column carries a non-empty description; else 0.0.
  spec/feature/BACKEND.md §Metrics Service §doc-health:
    - Sources table description from DatasetProperties.description, overlaid
      by EditableDatasetProperties.description when present.
    - Per-column descriptions from SchemaMetadata.fields[*].description,
      overlaid by EditableSchemaMetadata.editableSchemaFieldInfo[*].description.
  spec/feature/BACKEND.md §Metrics Service §Breakdown format:
    - datasets[] carries only failed entries (doc-health < 1.0).
    - No 'category' field.
    - metric_conf={} (no parameters).

DataHub is stubbed via _StubDH which implements get_dataset_documentation_aspects,
matching the batched GraphQL interface used by the doc-health measurer.

No-schema signal: when field_descriptions is empty the measurer treats the
dataset as having no documentable columns (score 0.0).
"""

from unittest.mock import AsyncMock

import pytest

from src.backend.metrics.measurers import doc_health  # noqa: F401 — triggers registration
from src.shared.datahub.client import DocumentationAspects


def _get_measurer():
    from src.backend.metrics.measurers.registry import get_measurer
    fn = get_measurer("doc-health")
    assert fn is not None, "doc-health measurer must be registered"
    return fn


# ── Stub DataHub client ───────────────────────────────────────────────────────


class _StubDH:
    """Stub DataHubClient implementing get_dataset_documentation_aspects.

    Returns a precomputed mapping from URN to DocumentationAspects.
    URNs absent from the mapping receive a fully-empty aspects sentinel,
    matching the behaviour of the real client for unresolved URNs.
    """

    _EMPTY = DocumentationAspects(
        table_description=None,
        editable_table_description=None,
        field_descriptions={},
        editable_field_descriptions={},
    )

    def __init__(self, aspects: dict[str, DocumentationAspects] | None = None) -> None:
        self._aspects: dict[str, DocumentationAspects] = aspects or {}

    async def get_dataset_documentation_aspects(
        self, urns: list[str]
    ) -> dict[str, DocumentationAspects]:
        return {urn: self._aspects.get(urn, self._EMPTY) for urn in urns}


# ── Registration ──────────────────────────────────────────────────────────────


def test_registered_under_correct_key() -> None:
    """Measurer is registered under 'doc-health'.

    Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types.
    """
    fn = _get_measurer()
    assert fn is not None


# ── Empty datasets list ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_datasets_returns_zeros() -> None:
    """measure([]) returns total=0.0, doc_health=0.0 with empty datasets list.

    Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types — total = count
          of datasets matched by dataset_filter.
    """
    measure = _get_measurer()

    values, breakdown = await measure(
        datasets=[],
        metric_conf={},
        datahub=_StubDH(),
        db=AsyncMock(),
    )

    assert values == {"total": 0.0, "doc_health": 0.0}
    assert breakdown["dataset_count"] == 0
    assert breakdown["datasets"] == []


# ── Dataset with full documentation → score 1.0 ───────────────────────────────


@pytest.mark.asyncio
async def test_fully_documented_dataset_not_in_breakdown() -> None:
    """Dataset with non-empty table description AND all columns described → score 1.0.

    Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types — a dataset scores
          1.0 iff it has a non-empty table description AND every column has a
          non-empty description.
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.well_documented,DEV)"

    dh = _StubDH({
        urn: DocumentationAspects(
            table_description="A well-documented table",
            editable_table_description=None,
            field_descriptions={"id": "Primary key", "name": "Full name of the record"},
            editable_field_descriptions={},
        )
    })

    values, breakdown = await measure(
        datasets=[urn],
        metric_conf={},
        datahub=dh,
        db=AsyncMock(),
    )

    assert values["total"] == 1.0
    assert values["doc_health"] == 1.0
    assert breakdown["datasets"] == [], (
        "Fully documented dataset must NOT appear in breakdown. "
        "Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types."
    )


# ── Missing table description → score 0.0 ────────────────────────────────────


@pytest.mark.asyncio
async def test_missing_table_description_in_breakdown() -> None:
    """Dataset with empty/missing table description scores 0.0 and is in breakdown.

    Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types — must have
          non-empty table description; else 0.0.
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.no_table_desc,DEV)"

    dh = _StubDH({
        urn: DocumentationAspects(
            table_description="",
            editable_table_description=None,
            field_descriptions={"id": "Primary key"},
            editable_field_descriptions={},
        )
    })

    values, breakdown = await measure(
        datasets=[urn],
        metric_conf={},
        datahub=dh,
        db=AsyncMock(),
    )

    assert values["doc_health"] == 0.0
    assert len(breakdown["datasets"]) == 1
    entry = breakdown["datasets"][0]
    assert entry["urn"] == urn
    assert "category" not in entry
    assert isinstance(entry.get("detail", {}), dict)


# ── Missing column description → score 0.0 ────────────────────────────────────


@pytest.mark.asyncio
async def test_missing_column_description_in_breakdown() -> None:
    """Dataset with one column having empty description scores 0.0 and is in breakdown.

    Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types — every column
          must have a non-empty description; else 0.0.
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.missing_col_desc,DEV)"

    dh = _StubDH({
        urn: DocumentationAspects(
            table_description="Has a table description",
            editable_table_description=None,
            # field_descriptions includes all schema fields; empty string = undescribed
            field_descriptions={"id": "Primary key", "notes": ""},
            editable_field_descriptions={},
        )
    })

    values, breakdown = await measure(
        datasets=[urn],
        metric_conf={},
        datahub=dh,
        db=AsyncMock(),
    )

    assert values["doc_health"] == 0.0
    assert len(breakdown["datasets"]) == 1
    entry = breakdown["datasets"][0]
    assert entry["urn"] == urn
    assert "category" not in entry
    assert isinstance(entry.get("detail", {}), dict)


# ── EditableDatasetProperties overrides base description ─────────────────────


@pytest.mark.asyncio
async def test_editable_table_description_overrides_base() -> None:
    """EditableDatasetProperties.description overrides empty DatasetProperties.description.

    Spec: spec/feature/BACKEND.md §Metrics Service §doc-health — resolve table
          description via DatasetProperties overlaid by EditableDatasetProperties
          when present.
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.editable_table,DEV)"

    dh = _StubDH({
        urn: DocumentationAspects(
            table_description="",
            editable_table_description="Editable table description",
            field_descriptions={"id": "Primary key"},
            editable_field_descriptions={},
        )
    })

    values, breakdown = await measure(
        datasets=[urn],
        metric_conf={},
        datahub=dh,
        db=AsyncMock(),
    )

    assert values["doc_health"] == 1.0, (
        "EditableDatasetProperties.description must override empty base description. "
        "Spec: spec/feature/BACKEND.md §Metrics Service §doc-health."
    )
    assert breakdown["datasets"] == []


# ── EditableSchemaMetadata overrides per-column base descriptions ─────────────


@pytest.mark.asyncio
async def test_editable_schema_overrides_empty_column_description() -> None:
    """EditableSchemaMetadata overrides an empty column base description → score 1.0.

    Spec: spec/feature/BACKEND.md §Metrics Service §doc-health — per-column
          descriptions from SchemaMetadata overlaid by EditableSchemaMetadata.
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.editable_schema,DEV)"

    dh = _StubDH({
        urn: DocumentationAspects(
            table_description="Table description",
            editable_table_description=None,
            # Base schema: "notes" is empty — will be overridden by editable overlay
            field_descriptions={"id": "Primary key", "notes": ""},
            editable_field_descriptions={"notes": "Editable column description"},
        )
    })

    values, breakdown = await measure(
        datasets=[urn],
        metric_conf={},
        datahub=dh,
        db=AsyncMock(),
    )

    assert values["doc_health"] == 1.0, (
        "EditableSchemaMetadata must override empty base column description. "
        "Spec: spec/feature/BACKEND.md §Metrics Service §doc-health."
    )
    assert breakdown["datasets"] == []


# ── metric_conf={} is accepted ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_metric_conf_is_accepted() -> None:
    """doc-health measurer accepts metric_conf={} (no parameters).

    Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types —
          metric_conf is {} for doc-health.
    """
    measure = _get_measurer()

    values, breakdown = await measure(
        datasets=[],
        metric_conf={},
        datahub=_StubDH(),
        db=AsyncMock(),
    )

    assert values["total"] == 0.0


# ── No 'category' in breakdown entries ───────────────────────────────────────


@pytest.mark.asyncio
async def test_breakdown_entries_have_no_category_field() -> None:
    """Breakdown entries must not carry a 'category' field.

    Spec: spec/feature/BACKEND.md §Metrics Service §Breakdown format —
          datasets[] entries are {urn, detail?} only.
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.nocat,DEV)"

    dh = _StubDH({
        urn: DocumentationAspects(
            table_description="",
            editable_table_description=None,
            field_descriptions={"id": ""},
            editable_field_descriptions={},
        )
    })

    values, breakdown = await measure(
        datasets=[urn],
        metric_conf={},
        datahub=dh,
        db=AsyncMock(),
    )

    assert len(breakdown["datasets"]) >= 1
    for entry in breakdown["datasets"]:
        assert "category" not in entry, (
            "Breakdown entry must not have 'category'. "
            "Spec: spec/feature/BACKEND.md §Metrics Service §Breakdown format."
        )
        assert "urn" in entry


# ── No schema metadata (empty field_descriptions) ────────────────────────────


@pytest.mark.asyncio
async def test_no_schema_metadata_scores_zero_and_in_breakdown() -> None:
    """Dataset with empty field_descriptions scores 0.0 and appears in breakdown.

    When field_descriptions is empty the measurer cannot satisfy "every column
    carries a non-empty description", so the score is 0.0.

    Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types — a dataset scores 1.0
          iff it has a non-empty table description AND every column carries a non-empty
          description.

    Note: spec is silent on the exact treatment of absent SchemaMetadata; this test
    encodes the impl's interpretation (score=0.0, dataset in breakdown) and is marked
    as an interpretation-of-spec rather than a direct spec citation.
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.no_schema,DEV)"

    dh = _StubDH({
        urn: DocumentationAspects(
            table_description="A table with no schema",
            editable_table_description=None,
            field_descriptions={},
            editable_field_descriptions={},
        )
    })

    values, breakdown = await measure(
        datasets=[urn],
        metric_conf={},
        datahub=dh,
        db=AsyncMock(),
    )

    assert values["total"] == 1.0
    # The impl returns 0.0 when field_descriptions is empty (no schema / empty schema).
    # Spec is silent on this exact case; this test pins the impl decision.
    # Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types — "every column
    # carries a non-empty description" cannot be satisfied with no columns.
    assert values["doc_health"] == 0.0
    # breakdown invariants
    assert len(breakdown["datasets"]) == 1
    assert breakdown["datasets"][0]["urn"] == urn
    assert "category" not in breakdown["datasets"][0]


# ── URN absent from DataHub batch response ────────────────────────────────────


@pytest.mark.asyncio
async def test_unresolved_urn_scores_zero() -> None:
    """A URN absent from the DataHub batch response scores 0.0 and is in the breakdown.

    The _StubDH returns an empty map for the URN, simulating the real client's behaviour
    for URNs that DataHub does not return (unresolved entities).  The measurer must treat
    absent aspects as a failed dataset (0.0).

    Spec: spec/feature/BACKEND.md §Metrics Service — URNs that don't resolve in
          DataHub at run time are accumulated into the run_complete event's
          unresolved_urns; the doc_health measurer treats them as failed.
    """
    measure = _get_measurer()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.unresolved.ghost,DEV)"

    # Empty mapping — URN is NOT in the precomputed aspects (simulates unresolved entity).
    values, breakdown = await measure(
        datasets=[urn],
        metric_conf={},
        datahub=_StubDH({}),
        db=AsyncMock(),
    )

    assert values == {"total": 1.0, "doc_health": 0.0}
    assert len(breakdown["datasets"]) == 1
    assert breakdown["datasets"][0]["urn"] == urn
