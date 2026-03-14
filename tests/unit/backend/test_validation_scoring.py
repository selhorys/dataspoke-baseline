"""Unit tests for compute_quality_score (mocked DataHub + Redis)."""

import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.backend.validation.scoring import WEIGHTS, compute_quality_score
from tests.unit.backend.conftest import make_mock_operation


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
    total = sum(WEIGHTS.values())
    assert abs(total - 1.0) < 1e-9


# ── Perfect score ─────────────────────────────────────────────────────────────


async def test_perfect_score(datahub, cache):
    """All aspects fully populated → score near 100."""
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
    assert score.overall_score >= 95
    assert score.dimensions["completeness"] == 100.0
    assert score.dimensions["ownership_tags"] == 100.0


# ── Zero score ────────────────────────────────────────────────────────────────


async def test_zero_score(datahub, cache):
    """All aspects missing → score 0."""
    datahub.get_aspect = AsyncMock(return_value=None)
    datahub.get_timeseries = AsyncMock(return_value=[])
    datahub.get_schema_version_list = AsyncMock(return_value=[])

    score = await compute_quality_score(datahub, "urn:test", cache=cache)
    assert score.overall_score == 0.0
    for dim_score in score.dimensions.values():
        assert dim_score == 0.0


# ── Completeness dimension ────────────────────────────────────────────────────


async def test_completeness_dimension(datahub, cache):
    """Schema with 50% described fields → completeness ≈ 50."""

    async def _get_aspect(urn, cls):
        if cls.__name__ == "SchemaMetadataClass":
            return _mock_schema(fields_with_desc=5, fields_total=10)
        return None

    datahub.get_aspect = _get_aspect
    datahub.get_timeseries = AsyncMock(return_value=[])
    datahub.get_schema_version_list = AsyncMock(return_value=[])

    score = await compute_quality_score(datahub, "urn:test", cache=cache)
    assert score.dimensions["completeness"] == 50.0


# ── Freshness dimension ──────────────────────────────────────────────────────


async def test_freshness_recent(datahub, cache):
    """Last operation 0 days ago → freshness 100."""
    now_ms = int(time.time() * 1000)
    datahub.get_aspect = AsyncMock(return_value=None)
    datahub.get_timeseries = AsyncMock(return_value=[make_mock_operation(now_ms)])
    datahub.get_schema_version_list = AsyncMock(return_value=[])

    score = await compute_quality_score(datahub, "urn:test", cache=cache)
    assert score.dimensions["freshness"] == 100.0


async def test_freshness_stale(datahub, cache):
    """Last operation 30+ days ago → freshness 0."""
    old_ms = int((time.time() - 31 * 86400) * 1000)
    datahub.get_aspect = AsyncMock(return_value=None)
    datahub.get_timeseries = AsyncMock(return_value=[make_mock_operation(old_ms)])
    datahub.get_schema_version_list = AsyncMock(return_value=[])

    score = await compute_quality_score(datahub, "urn:test", cache=cache)
    assert score.dimensions["freshness"] == 0.0


# ── Schema stability ─────────────────────────────────────────────────────────


async def test_schema_stability_no_changes(datahub, cache):
    """Single version (initial) → stability 100, zero changes."""
    now_ms = int(time.time() * 1000)
    datahub.get_aspect = AsyncMock(return_value=None)
    datahub.get_timeseries = AsyncMock(return_value=[])
    datahub.get_schema_version_list = AsyncMock(
        return_value=_make_version_list([("0.0.0-computed", now_ms)])
    )

    score = await compute_quality_score(datahub, "urn:test", cache=cache)
    assert score.dimensions["schema_stability"] == 100.0
    details = score.dimension_details["schema_stability"]
    assert details["major_changes"] == 0
    assert details["minor_changes"] == 0


async def test_schema_stability_no_schema(datahub, cache):
    """No schema versions → stability 0, no details."""
    datahub.get_aspect = AsyncMock(return_value=None)
    datahub.get_timeseries = AsyncMock(return_value=[])
    datahub.get_schema_version_list = AsyncMock(return_value=[])

    score = await compute_quality_score(datahub, "urn:test", cache=cache)
    assert score.dimensions["schema_stability"] == 0.0
    assert score.dimension_details is None


async def test_schema_stability_minor_changes(datahub, cache):
    """3 minor changes in last 30 days → score 97 (100 - 3*1)."""
    now_ms = int(time.time() * 1000)
    day_ms = 86400 * 1000
    versions = _make_version_list(
        [
            ("0.0.0-computed", now_ms - 20 * day_ms),
            ("0.1.0-computed", now_ms - 15 * day_ms),
            ("0.2.0-computed", now_ms - 10 * day_ms),
            ("0.3.0-computed", now_ms - 5 * day_ms),
        ]
    )
    datahub.get_aspect = AsyncMock(return_value=None)
    datahub.get_timeseries = AsyncMock(return_value=[])
    datahub.get_schema_version_list = AsyncMock(return_value=versions)

    score = await compute_quality_score(datahub, "urn:test", cache=cache)
    assert score.dimensions["schema_stability"] == 97.0
    details = score.dimension_details["schema_stability"]
    assert details["major_changes"] == 0
    assert details["minor_changes"] == 3


async def test_schema_stability_major_changes(datahub, cache):
    """2 major changes in last 30 days → score 80 (100 - 2*10)."""
    now_ms = int(time.time() * 1000)
    day_ms = 86400 * 1000
    versions = _make_version_list(
        [
            ("0.0.0-computed", now_ms - 25 * day_ms),
            ("1.0.0-computed", now_ms - 15 * day_ms),
            ("2.0.0-computed", now_ms - 5 * day_ms),
        ]
    )
    datahub.get_aspect = AsyncMock(return_value=None)
    datahub.get_timeseries = AsyncMock(return_value=[])
    datahub.get_schema_version_list = AsyncMock(return_value=versions)

    score = await compute_quality_score(datahub, "urn:test", cache=cache)
    assert score.dimensions["schema_stability"] == 80.0
    details = score.dimension_details["schema_stability"]
    assert details["major_changes"] == 2
    assert details["minor_changes"] == 0


async def test_schema_stability_mixed_changes(datahub, cache):
    """1 major + 2 minor in last 30 days → score 88 (100 - 10 - 2)."""
    now_ms = int(time.time() * 1000)
    day_ms = 86400 * 1000
    versions = _make_version_list(
        [
            ("0.0.0-computed", now_ms - 25 * day_ms),
            ("0.1.0-computed", now_ms - 20 * day_ms),
            ("0.2.0-computed", now_ms - 15 * day_ms),
            ("1.0.0-computed", now_ms - 5 * day_ms),
        ]
    )
    datahub.get_aspect = AsyncMock(return_value=None)
    datahub.get_timeseries = AsyncMock(return_value=[])
    datahub.get_schema_version_list = AsyncMock(return_value=versions)

    score = await compute_quality_score(datahub, "urn:test", cache=cache)
    assert score.dimensions["schema_stability"] == 88.0
    details = score.dimension_details["schema_stability"]
    assert details["major_changes"] == 1
    assert details["minor_changes"] == 2


async def test_schema_stability_old_changes_ignored(datahub, cache):
    """Changes older than 30 days don't count."""
    now_ms = int(time.time() * 1000)
    day_ms = 86400 * 1000
    versions = _make_version_list(
        [
            ("0.0.0-computed", now_ms - 60 * day_ms),
            ("1.0.0-computed", now_ms - 45 * day_ms),  # older than 30d → ignored
            ("2.0.0-computed", now_ms - 5 * day_ms),  # within 30d → counts
        ]
    )
    datahub.get_aspect = AsyncMock(return_value=None)
    datahub.get_timeseries = AsyncMock(return_value=[])
    datahub.get_schema_version_list = AsyncMock(return_value=versions)

    score = await compute_quality_score(datahub, "urn:test", cache=cache)
    assert score.dimensions["schema_stability"] == 90.0
    details = score.dimension_details["schema_stability"]
    assert details["major_changes"] == 1
    assert details["minor_changes"] == 0


async def test_schema_stability_floor_at_zero(datahub, cache):
    """Many changes clamp score at 0, not negative."""
    now_ms = int(time.time() * 1000)
    day_ms = 86400 * 1000
    versions = _make_version_list(
        [(f"{i}.0.0-computed", now_ms - (25 - i) * day_ms) for i in range(15)]
    )
    datahub.get_aspect = AsyncMock(return_value=None)
    datahub.get_timeseries = AsyncMock(return_value=[])
    datahub.get_schema_version_list = AsyncMock(return_value=versions)

    score = await compute_quality_score(datahub, "urn:test", cache=cache)
    assert score.dimensions["schema_stability"] == 0.0


# ── Data quality dimension ────────────────────────────────────────────────────


async def test_data_quality_good(datahub, cache):
    """Low null ratio → high score."""
    datahub.get_aspect = AsyncMock(return_value=None)
    datahub.get_schema_version_list = AsyncMock(return_value=[])

    async def _get_timeseries(urn, cls, limit=30):
        if cls.__name__ == "DatasetProfileClass":
            return [_mock_profile(null_proportions=[0.01, 0.02], row_count=1000)]
        return []

    datahub.get_timeseries = _get_timeseries

    score = await compute_quality_score(datahub, "urn:test", cache=cache)
    assert score.dimensions["data_quality"] >= 95.0


async def test_data_quality_poor(datahub, cache):
    """High null ratio → low score."""
    datahub.get_aspect = AsyncMock(return_value=None)
    datahub.get_schema_version_list = AsyncMock(return_value=[])

    async def _get_timeseries(urn, cls, limit=30):
        if cls.__name__ == "DatasetProfileClass":
            return [_mock_profile(null_proportions=[0.9, 0.8], row_count=1000)]
        return []

    datahub.get_timeseries = _get_timeseries

    score = await compute_quality_score(datahub, "urn:test", cache=cache)
    assert score.dimensions["data_quality"] <= 20.0


# ── Ownership & tags ──────────────────────────────────────────────────────────


async def test_ownership_tags_full(datahub, cache):
    """Has owner + tags → 100."""

    async def _get_aspect(urn, cls):
        if cls.__name__ == "OwnershipClass":
            return _mock_ownership(has_owners=True)
        if cls.__name__ == "GlobalTagsClass":
            return _mock_tags(has_tags=True)
        return None

    datahub.get_aspect = _get_aspect
    datahub.get_timeseries = AsyncMock(return_value=[])
    datahub.get_schema_version_list = AsyncMock(return_value=[])

    score = await compute_quality_score(datahub, "urn:test", cache=cache)
    assert score.dimensions["ownership_tags"] == 100.0


async def test_ownership_tags_none(datahub, cache):
    """Missing both → 0."""
    datahub.get_aspect = AsyncMock(return_value=None)
    datahub.get_timeseries = AsyncMock(return_value=[])
    datahub.get_schema_version_list = AsyncMock(return_value=[])

    score = await compute_quality_score(datahub, "urn:test", cache=cache)
    assert score.dimensions["ownership_tags"] == 0.0


# ── Cache behaviour ──────────────────────────────────────────────────────────


async def test_cache_hit(datahub, cache):
    """Score returned from Redis cache without calling DataHub."""
    cached_data = json.dumps(
        {
            "overall_score": 85.0,
            "dimensions": {
                "completeness": 90.0,
                "freshness": 80.0,
                "schema_stability": 100.0,
                "data_quality": 70.0,
                "ownership_tags": 100.0,
            },
            "dimension_details": {
                "schema_stability": {"major_changes": 0, "minor_changes": 0},
            },
        }
    )
    cache.get = AsyncMock(return_value=cached_data)

    score = await compute_quality_score(datahub, "urn:test", cache=cache)
    assert score.overall_score == 85.0
    assert score.dimension_details["schema_stability"]["major_changes"] == 0
    datahub.get_aspect.assert_not_awaited()


async def test_cache_miss_then_set(datahub, cache):
    """Cache miss → compute → write to cache."""
    cache.get = AsyncMock(return_value=None)
    datahub.get_aspect = AsyncMock(return_value=None)
    datahub.get_timeseries = AsyncMock(return_value=[])
    datahub.get_schema_version_list = AsyncMock(return_value=[])

    score = await compute_quality_score(datahub, "urn:test", cache=cache)
    assert score.overall_score == 0.0
    cache.set.assert_awaited_once()
