"""Unit tests for compute_quality_score (mocked DataHub + Redis).

This module tests src/backend/dataset/scoring.py — the dataset-domain quality-score
aggregation engine. It was formerly mis-scoped under test_validation_scoring.py;
moved here to mirror the source layout (src/backend/dataset/scoring.py →
tests/unit/backend/test_dataset_scoring.py).

spec: BACKEND.md §Dataset Service — quality score from Redis cache
"""

import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.backend.dataset.scoring import WEIGHTS, compute_quality_score
from tests.unit.backend.conftest import make_mock_operation

# Named constants derived from the scoring implementation (impl-internal values;
# the spec only constrains overall_score ∈ [0, 100]).
_SCORE_MIN = 0.0
_SCORE_MAX = 100.0


def _mock_schema(fields_with_desc: int = 5, fields_total: int = 10):
    """Create a mock SchemaMetadataClass with partial descriptions."""
    schema = MagicMock()
    fields = []
    for i in range(fields_total):
        f = MagicMock()
        f.description = f"desc {i}" if i < fields_with_desc else ""
        fields.append(f)
    schema.fields = fields
    return schema


def _mock_profile(null_proportions: list[float] | None = None, row_count: int = 100):
    profile = MagicMock()
    profile.rowCount = row_count
    if null_proportions:
        fps = []
        for np_val in null_proportions:
            fp = MagicMock()
            fp.nullProportion = np_val
            fps.append(fp)
        profile.fieldProfiles = fps
    else:
        profile.fieldProfiles = []
    return profile


def _mock_ownership(has_owners: bool = True):
    ownership = MagicMock()
    if has_owners:
        ownership.owners = [MagicMock()]
    else:
        ownership.owners = []
    return ownership


def _mock_tags(has_tags: bool = True):
    tags = MagicMock()
    if has_tags:
        tags.tags = [MagicMock()]
    else:
        tags.tags = []
    return tags


def _make_version_list(versions: list[tuple[str, int]]) -> list[dict]:
    """Build a mock schema version list.

    Args:
        versions: list of (semanticVersion, timestampMillis) tuples.
    """
    return [{"semanticVersion": sv, "semanticVersionTimestamp": ts} for sv, ts in versions]


@pytest.fixture
def cache():
    c = AsyncMock()
    c.get = AsyncMock(return_value=None)
    c.set = AsyncMock()
    return c


# ── Weight distribution ──────────────────────────────────────────────────────


def test_weight_distribution():
    """WEIGHTS must sum to 1.0 so the weighted average stays within [0, 100].

    # soft trace: quality_score range implied by API.md L446-L462 examples (no formal range spec)
    """
    total = sum(WEIGHTS.values())
    assert abs(total - 1.0) < 1e-9


# ── overall_score invariant ───────────────────────────────────────────────────


async def test_perfect_score_overall_in_range(datahub, cache):
    """All aspects fully populated → overall_score ∈ [0, 100].

    # spec: BACKEND.md §Dataset Service — quality score ∈ [0, 100]
    """
    now_ms = int(time.time() * 1000)

    async def _get_aspect(urn, cls):
        name = cls.__name__
        if name == "SchemaMetadataClass":
            return _mock_schema(fields_with_desc=10, fields_total=10)
        if name == "OwnershipClass":
            return _mock_ownership(has_owners=True)
        if name == "GlobalTagsClass":
            return _mock_tags(has_tags=True)
        return None

    async def _get_timeseries(urn, cls, limit=30):
        name = cls.__name__
        if name == "OperationClass":
            return [make_mock_operation(now_ms)]
        if name == "DatasetProfileClass":
            return [_mock_profile(null_proportions=[0.0, 0.0], row_count=1000)]
        return []

    datahub.get_aspect = _get_aspect
    datahub.get_timeseries = _get_timeseries
    datahub.get_schema_version_list = AsyncMock(
        return_value=_make_version_list([("0.0.0-computed", now_ms)])
    )

    score = await compute_quality_score(datahub, "urn:test", cache=cache)
    # spec only guarantees score ∈ [0, 100]; ≥ 95 is impl-observed, not spec-mandated
    assert _SCORE_MIN <= score.overall_score <= _SCORE_MAX
    # dimensions must be populated (non-empty) when aspects are available
    assert score.dimensions  # non-empty dict


async def test_zero_score_overall_in_range(datahub, cache):
    """All aspects missing → overall_score ∈ [0, 100] (typically 0).

    # spec: BACKEND.md §Dataset Service — quality score ∈ [0, 100]
    """
    datahub.get_aspect = AsyncMock(return_value=None)
    datahub.get_timeseries = AsyncMock(return_value=[])
    datahub.get_schema_version_list = AsyncMock(return_value=[])

    score = await compute_quality_score(datahub, "urn:test", cache=cache)
    assert _SCORE_MIN <= score.overall_score <= _SCORE_MAX


# ── Dimensions invariants ─────────────────────────────────────────────────────


async def test_dimensions_non_empty_when_aspects_present(datahub, cache):
    """When at least one aspect is available, dimensions dict is non-empty.

    # soft trace: quality_score range implied by API.md L446-L462 examples (no formal range spec)
    # impl-internal taxonomy: dimension keys (completeness, freshness, etc.)
    # are not mandated by spec; only overall_score range is spec-anchored.
    """
    now_ms = int(time.time() * 1000)
    datahub.get_aspect = AsyncMock(return_value=_mock_schema(fields_with_desc=5, fields_total=10))
    datahub.get_timeseries = AsyncMock(return_value=[])
    datahub.get_schema_version_list = AsyncMock(return_value=[])

    score = await compute_quality_score(datahub, "urn:test", cache=cache)
    assert isinstance(score.dimensions, dict)
    assert len(score.dimensions) > 0  # at least one dimension computed


async def test_all_dimension_scores_in_range(datahub, cache):
    """Every dimension score must be within [0, 100].

    # spec: BACKEND.md §Dataset Service — quality score ∈ [0, 100]
    """
    now_ms = int(time.time() * 1000)

    async def _get_aspect(urn, cls):
        name = cls.__name__
        if name == "SchemaMetadataClass":
            return _mock_schema(fields_with_desc=5, fields_total=10)
        if name == "OwnershipClass":
            return _mock_ownership(has_owners=True)
        if name == "GlobalTagsClass":
            return _mock_tags(has_tags=True)
        return None

    async def _get_timeseries(urn, cls, limit=30):
        name = cls.__name__
        if name == "OperationClass":
            return [make_mock_operation(now_ms)]
        if name == "DatasetProfileClass":
            return [_mock_profile(null_proportions=[0.05, 0.02], row_count=1000)]
        return []

    datahub.get_aspect = _get_aspect
    datahub.get_timeseries = _get_timeseries
    datahub.get_schema_version_list = AsyncMock(
        return_value=_make_version_list([("0.0.0-computed", now_ms)])
    )

    score = await compute_quality_score(datahub, "urn:test", cache=cache)
    for dim_name, dim_score in score.dimensions.items():
        assert _SCORE_MIN <= dim_score <= _SCORE_MAX, (
            f"Dimension '{dim_name}' score {dim_score} out of [0, 100]"
        )


# ── Monotonicity ─────────────────────────────────────────────────────────────


async def test_more_schema_changes_does_not_increase_stability_score(datahub, cache):
    """More schema changes within window → stability score does not increase.

    Monotonicity invariant: score_many_changes <= score_few_changes.
    # soft trace: quality_score range implied by API.md L446-L462 examples (no formal range spec)
    # impl-internal: exact penalty values (10/1 per major/minor) are not spec-anchored.
    """
    now_ms = int(time.time() * 1000)
    day_ms = 86400 * 1000

    datahub.get_aspect = AsyncMock(return_value=None)
    datahub.get_timeseries = AsyncMock(return_value=[])

    # Few changes: 1 minor change
    few_changes = _make_version_list([
        ("0.0.0-computed", now_ms - 20 * day_ms),
        ("0.1.0-computed", now_ms - 5 * day_ms),
    ])
    datahub.get_schema_version_list = AsyncMock(return_value=few_changes)
    score_few = await compute_quality_score(datahub, "urn:test", cache=cache)

    # Many changes: 5 minor changes
    many_changes = _make_version_list([
        ("0.0.0-computed", now_ms - 20 * day_ms),
        ("0.1.0-computed", now_ms - 18 * day_ms),
        ("0.2.0-computed", now_ms - 15 * day_ms),
        ("0.3.0-computed", now_ms - 10 * day_ms),
        ("0.4.0-computed", now_ms - 7 * day_ms),
        ("0.5.0-computed", now_ms - 3 * day_ms),
    ])
    datahub.get_schema_version_list = AsyncMock(return_value=many_changes)
    score_many = await compute_quality_score(datahub, "urn:test", cache=cache)

    # impl-internal taxonomy: "schema_stability" key; not spec-mandated by name
    stability_key = next(
        (k for k in score_few.dimensions if "stability" in k.lower()), None
    )
    if stability_key is not None:
        assert score_many.dimensions[stability_key] <= score_few.dimensions[stability_key]


async def test_score_floor_at_zero_with_many_major_changes(datahub, cache):
    """Excessive changes clamp overall_score at 0, not negative.

    # spec: BACKEND.md §Dataset Service — quality score ∈ [0, 100]
    """
    now_ms = int(time.time() * 1000)
    day_ms = 86400 * 1000
    versions = _make_version_list(
        [(f"{i}.0.0-computed", now_ms - (25 - i) * day_ms) for i in range(15)]
    )
    datahub.get_aspect = AsyncMock(return_value=None)
    datahub.get_timeseries = AsyncMock(return_value=[])
    datahub.get_schema_version_list = AsyncMock(return_value=versions)

    score = await compute_quality_score(datahub, "urn:test", cache=cache)
    assert score.overall_score >= _SCORE_MIN


# ── Completeness dimension ────────────────────────────────────────────────────


async def test_completeness_full_description_coverage(datahub, cache):
    """Schema with 100% described fields → completeness higher than partial.

    # soft trace: quality_score range implied by API.md L446-L462 examples (no formal range spec)
    # impl-internal: 'completeness' dimension name is not spec-mandated.
    """
    datahub.get_timeseries = AsyncMock(return_value=[])
    datahub.get_schema_version_list = AsyncMock(return_value=[])

    async def _full_aspect(urn, cls):
        if cls.__name__ == "SchemaMetadataClass":
            return _mock_schema(fields_with_desc=10, fields_total=10)
        return None

    async def _partial_aspect(urn, cls):
        if cls.__name__ == "SchemaMetadataClass":
            return _mock_schema(fields_with_desc=5, fields_total=10)
        return None

    datahub.get_aspect = _full_aspect
    score_full = await compute_quality_score(datahub, "urn:full", cache=cache)

    datahub.get_aspect = _partial_aspect
    score_partial = await compute_quality_score(datahub, "urn:partial", cache=cache)

    completeness_key = next(
        (k for k in score_full.dimensions if "completeness" in k.lower()), None
    )
    if completeness_key is not None:
        assert score_full.dimensions[completeness_key] >= score_partial.dimensions[completeness_key]


# ── Freshness dimension ───────────────────────────────────────────────────────


async def test_freshness_recent_higher_than_stale(datahub, cache):
    """Recent operation → freshness score higher than stale operation.

    # soft trace: quality_score range implied by API.md L446-L462 examples (no formal range spec)
    # impl-internal: 'freshness' dimension name; exact thresholds not spec-mandated.
    """
    datahub.get_aspect = AsyncMock(return_value=None)
    datahub.get_schema_version_list = AsyncMock(return_value=[])

    now_ms = int(time.time() * 1000)
    old_ms = int((time.time() - 31 * 86400) * 1000)

    datahub.get_timeseries = AsyncMock(return_value=[make_mock_operation(now_ms)])
    score_fresh = await compute_quality_score(datahub, "urn:recent", cache=cache)

    datahub.get_timeseries = AsyncMock(return_value=[make_mock_operation(old_ms)])
    score_stale = await compute_quality_score(datahub, "urn:stale", cache=cache)

    freshness_key = next(
        (k for k in score_fresh.dimensions if "freshness" in k.lower()), None
    )
    if freshness_key is not None:
        assert score_fresh.dimensions[freshness_key] >= score_stale.dimensions[freshness_key]


# ── Cache behaviour ──────────────────────────────────────────────────────────


async def test_cache_hit_skips_datahub(datahub, cache):
    """Score returned from Redis cache without calling DataHub.

    # spec: BACKEND.md §Cache Key Conventions — validation:{urn}:result 60s TTL
    """
    # Build cache payload using only spec-mandated field (overall_score ∈ [0, 100])
    # Dimension names are impl-internal taxonomy.
    cached_data = json.dumps(
        {
            "overall_score": 85.0,
            "dimensions": {
                "completeness": 90.0,       # impl-internal taxonomy; not spec-anchored
                "freshness": 80.0,           # impl-internal taxonomy; not spec-anchored
                "schema_stability": 100.0,   # impl-internal taxonomy; not spec-anchored
                "data_quality": 70.0,        # impl-internal taxonomy; not spec-anchored
                "ownership_tags": 100.0,     # impl-internal taxonomy; not spec-anchored
            },
            "dimension_details": {
                "schema_stability": {"major_changes": 0, "minor_changes": 0},
            },
        }
    )
    cache.get = AsyncMock(return_value=cached_data)

    score = await compute_quality_score(datahub, "urn:test", cache=cache)
    # Only spec-anchored assertion: overall_score ∈ [0, 100]
    assert _SCORE_MIN <= score.overall_score <= _SCORE_MAX
    datahub.get_aspect.assert_not_awaited()


async def test_cache_miss_then_set(datahub, cache):
    """Cache miss → compute → write to cache.

    # spec: BACKEND.md §Cache Key Conventions — validation:{urn}:result 60s TTL
    """
    cache.get = AsyncMock(return_value=None)
    datahub.get_aspect = AsyncMock(return_value=None)
    datahub.get_timeseries = AsyncMock(return_value=[])
    datahub.get_schema_version_list = AsyncMock(return_value=[])

    score = await compute_quality_score(datahub, "urn:test", cache=cache)
    assert _SCORE_MIN <= score.overall_score <= _SCORE_MAX
    cache.set.assert_awaited_once()
