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
  spec/feature/BACKEND.md §Metrics Service §Verdict contract:
    - The measurer returns (values, verdicts); verdicts cover EVERY dataset in
      scope, one entry per dataset carrying urn, met, evidence_at, detail.
    - evidence_at is None for doc-health — "a documentation state carries no
      timestamp" — so the endpoint falls back to the run's measured_at.
    - The failures-only metric_results.breakdown is DERIVED from the verdicts by
      MetricsService, not built here (see tests/unit/backend/metrics/test_service.py).
    - metric_conf={} (no parameters).

DataHub is stubbed via _StubDH which implements get_dataset_documentation_aspects,
matching the batched GraphQL interface used by the doc-health measurer.

No-schema signal: when field_descriptions is empty the measurer treats the
dataset as having no documentable columns (score 0.0).
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from src.backend.metrics.measurers import doc_health  # noqa: F401 — triggers registration
from src.shared.datahub.client import DocumentationAspects

#: The run's measurement instant. doc-health dates nothing against it — "a
#: documentation state carries no timestamp" — so a fixed value serves every call in
#: this file and no assertion here depends on which instant it is. It is passed all the
#: same: the parameter list is uniform across measurers.
#: Spec: spec/feature/BACKEND.md §Metrics Service — Measurers ("Each measurer receives
#: the resolved dataset URN list, `metric_conf`, the run's measurement instant (above),
#: a `DataHubClient`, and an `AsyncSession`") and §Verdict contract ("`evidence_at` is
#: … `None`, since a documentation state carries no timestamp").
_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


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


# ── Verdict helpers ───────────────────────────────────────────────────────────


def _verdict(verdicts, urn):
    """The one verdict for *urn* — verdicts cover every dataset exactly once.

    Spec: spec/feature/BACKEND.md §Metrics Service §Verdict contract — "`verdicts`
    covers **every** dataset in scope, not only the failing ones — one entry per
    dataset".
    """
    matches = [v for v in verdicts if v.urn == urn]
    assert len(matches) == 1, f"expected exactly one verdict for {urn}; got {matches!r}"
    return matches[0]


def _failed(verdicts):
    """The `met = false` subset — the entries the derived breakdown lists."""
    return [v for v in verdicts if not v.met]

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

    values, verdicts = await measure(
        datasets=[],
        metric_conf={},
        datahub=_StubDH(),
        db=AsyncMock(),
        now=_NOW,
    )

    assert values == {"total": 0.0, "doc_health": 0.0}
    assert verdicts == []


# ── Dataset with full documentation → score 1.0 ───────────────────────────────


@pytest.mark.asyncio
async def test_fully_documented_dataset_meets_the_criterion() -> None:
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

    values, verdicts = await measure(
        datasets=[urn],
        metric_conf={},
        datahub=dh,
        db=AsyncMock(),
        now=_NOW,
    )

    assert values["total"] == 1.0
    assert values["doc_health"] == 1.0
    assert _verdict(verdicts, urn).met is True, (
        "A fully documented dataset must carry a met verdict — it is still in scope, "
        "so it gets a verdict rather than being omitted. "
        "Spec: spec/feature/BACKEND.md §Metrics Service §Verdict contract."
    )
    assert _failed(verdicts) == [], "nothing failed, so the derived breakdown is empty"


# ── Missing table description → score 0.0 ────────────────────────────────────


@pytest.mark.asyncio
async def test_missing_table_description_fails_the_criterion() -> None:
    """Dataset with empty/missing table description scores 0.0 and fails its verdict.

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

    values, verdicts = await measure(
        datasets=[urn],
        metric_conf={},
        datahub=dh,
        db=AsyncMock(),
        now=_NOW,
    )

    assert values["doc_health"] == 0.0
    verdict = _verdict(verdicts, urn)
    assert verdict.met is False
    assert verdict.detail["missing_table_description"] is True
    assert _failed(verdicts) == [verdict]


# ── Missing column description → score 0.0 ────────────────────────────────────


@pytest.mark.asyncio
async def test_missing_column_description_fails_the_criterion() -> None:
    """Dataset with one column having empty description scores 0.0 and fails its verdict.

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

    values, verdicts = await measure(
        datasets=[urn],
        metric_conf={},
        datahub=dh,
        db=AsyncMock(),
        now=_NOW,
    )

    assert values["doc_health"] == 0.0
    verdict = _verdict(verdicts, urn)
    assert verdict.met is False
    assert verdict.detail["missing_column_descriptions"] == ["notes"]
    assert _failed(verdicts) == [verdict]


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

    values, verdicts = await measure(
        datasets=[urn],
        metric_conf={},
        datahub=dh,
        db=AsyncMock(),
        now=_NOW,
    )

    assert values["doc_health"] == 1.0, (
        "EditableDatasetProperties.description must override empty base description. "
        "Spec: spec/feature/BACKEND.md §Metrics Service §doc-health."
    )
    assert _verdict(verdicts, urn).met is True


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

    values, verdicts = await measure(
        datasets=[urn],
        metric_conf={},
        datahub=dh,
        db=AsyncMock(),
        now=_NOW,
    )

    assert values["doc_health"] == 1.0, (
        "EditableSchemaMetadata must override empty base column description. "
        "Spec: spec/feature/BACKEND.md §Metrics Service §doc-health."
    )
    assert _verdict(verdicts, urn).met is True


# ── metric_conf={} is accepted ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_metric_conf_is_accepted() -> None:
    """doc-health measurer accepts metric_conf={} (no parameters).

    Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types —
          metric_conf is {} for doc-health.
    """
    measure = _get_measurer()

    values, verdicts = await measure(
        datasets=[],
        metric_conf={},
        datahub=_StubDH(),
        db=AsyncMock(),
        now=_NOW,
    )

    assert values["total"] == 0.0


# ── Verdict field set ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_verdict_carries_exactly_the_four_contract_fields() -> None:
    """A verdict is {urn, met, evidence_at, detail} — no classification field.

    doc-health's evidence_at is None by contract, which is what makes the endpoint
    fall back to the run's measured_at for this type.

    Spec: spec/feature/BACKEND.md §Metrics Service §Verdict contract — "one entry per
          dataset carrying `urn`, `met: bool`, `evidence_at: datetime | None`, and a
          type-specific `detail`"; "`doc-health` → `None`, since a documentation state
          carries no timestamp".
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

    values, verdicts = await measure(
        datasets=[urn],
        metric_conf={},
        datahub=dh,
        db=AsyncMock(),
        now=_NOW,
    )

    from dataclasses import fields

    verdict = _verdict(verdicts, urn)
    assert {f.name for f in fields(verdict)} == {"urn", "met", "evidence_at", "detail"}
    assert verdict.met is False, "backstop: this dataset must actually have failed"
    assert verdict.evidence_at is None, (
        "doc-health carries no per-dataset timestamp. "
        "Spec: spec/feature/BACKEND.md §Metrics Service §Verdict contract."
    )


# ── No schema metadata (empty field_descriptions) ────────────────────────────


@pytest.mark.asyncio
async def test_no_schema_metadata_scores_zero_and_fails_its_verdict() -> None:
    """Dataset with empty field_descriptions scores 0.0 and fails its verdict.

    When field_descriptions is empty the measurer cannot satisfy "every column
    carries a non-empty description", so the score is 0.0.

    Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types — a dataset scores 1.0
          iff it has a non-empty table description AND every column carries a non-empty
          description.

    Note: spec is silent on the exact treatment of absent SchemaMetadata; this test
    encodes the impl's interpretation (score=0.0, met=false) and is marked
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

    values, verdicts = await measure(
        datasets=[urn],
        metric_conf={},
        datahub=dh,
        db=AsyncMock(),
        now=_NOW,
    )

    assert values["total"] == 1.0
    # The impl returns 0.0 when field_descriptions is empty (no schema / empty schema).
    # Spec is silent on this exact case; this test pins the impl decision.
    # Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types — "every column
    # carries a non-empty description" cannot be satisfied with no columns.
    assert values["doc_health"] == 0.0
    assert _verdict(verdicts, urn).met is False


# ── URN absent from DataHub batch response ────────────────────────────────────


@pytest.mark.asyncio
async def test_unresolved_urn_scores_zero() -> None:
    """A URN absent from the DataHub batch response scores 0.0 and fails its verdict.

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
    values, verdicts = await measure(
        datasets=[urn],
        metric_conf={},
        datahub=_StubDH({}),
        db=AsyncMock(),
        now=_NOW,
    )

    assert values == {"total": 1.0, "doc_health": 0.0}
    assert _verdict(verdicts, urn).met is False


# ── Verdicts cover every dataset in scope ─────────────────────────────────────


@pytest.mark.asyncio
async def test_verdicts_cover_every_dataset_passing_and_failing_alike() -> None:
    """Both a passing and a failing dataset get a verdict, in scope order.

    Full coverage is the whole point of the contract: it is what lets
    `GET /spoke/governance/metric/{id}/dataset` tell "evaluated and passing" from
    "in scope but never evaluated" (`unknown`), which a failures-only return cannot.

    Spec: spec/feature/BACKEND.md §Metrics Service §Verdict contract — "`verdicts`
          covers every dataset the measurer **evaluated**, not only the failing ones …
          Covering the passing datasets too is what makes 'in scope but never evaluated'
          (`unknown`) distinguishable from 'evaluated and passing': a failures-only
          return cannot express the difference." For `doc-health` the evaluated set is
          the whole scope.
    """
    measure = _get_measurer()
    good = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.good,DEV)"
    bad = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.bad,DEV)"

    dh = _StubDH({
        good: DocumentationAspects(
            table_description="Described",
            editable_table_description=None,
            field_descriptions={"id": "Primary key"},
            editable_field_descriptions={},
        ),
        bad: DocumentationAspects(
            table_description="",
            editable_table_description=None,
            field_descriptions={"id": "Primary key"},
            editable_field_descriptions={},
        ),
    })

    values, verdicts = await measure(
        datasets=[good, bad],
        metric_conf={},
        datahub=dh,
        db=AsyncMock(),
        now=_NOW,
    )

    assert values == {"total": 2.0, "doc_health": 1.0}
    assert [v.urn for v in verdicts] == [good, bad]
    assert [v.met for v in verdicts] == [True, False]
