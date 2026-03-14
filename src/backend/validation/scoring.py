"""Quality score engine — computes a 0-100 composite score from DataHub aspects."""

import json
import re
from datetime import UTC, datetime
from typing import Any

from src.shared.cache.client import QUALITY_CACHE_KEY, RedisClient
from src.shared.config import QUALITY_SCORE_CACHE_TTL
from src.shared.datahub.client import DataHubClient
from src.shared.models.quality import QualityScore

_SCHEMA_STABILITY_WINDOW_DAYS = 30
_MAJOR_CHANGE_PENALTY = 10
_MINOR_CHANGE_PENALTY = 1

# Dimension weights (must sum to 1.0)
WEIGHTS: dict[str, float] = {
    "completeness": 0.25,
    "freshness": 0.25,
    "schema_stability": 0.15,
    "data_quality": 0.20,
    "ownership_tags": 0.15,
}


async def compute_quality_score(
    datahub: DataHubClient,
    dataset_urn: str,
    cache: RedisClient | None = None,
) -> QualityScore:
    """Compute a 0-100 quality score from DataHub aspects, with optional Redis caching."""
    cache_key = QUALITY_CACHE_KEY.format(dataset_urn=dataset_urn)

    if cache is not None:
        cached = await cache.get(cache_key)
        if cached is not None:
            data = json.loads(cached)
            return QualityScore(
                overall_score=data["overall_score"],
                dimensions=data["dimensions"],
                dimension_details=data.get("dimension_details"),
            )

    stability_score, stability_details = await _score_schema_stability(datahub, dataset_urn)

    dimensions: dict[str, float] = {
        "completeness": await _score_completeness(datahub, dataset_urn),
        "freshness": await _score_freshness(datahub, dataset_urn),
        "schema_stability": stability_score,
        "data_quality": await _score_data_quality(datahub, dataset_urn),
        "ownership_tags": await _score_ownership_tags(datahub, dataset_urn),
    }

    dimension_details: dict[str, dict[str, Any]] | None = None
    if stability_details:
        dimension_details = {"schema_stability": stability_details}

    overall = sum(dimensions[k] * WEIGHTS[k] for k in WEIGHTS)
    overall = round(min(max(overall, 0.0), 100.0), 2)

    score = QualityScore(
        overall_score=overall,
        dimensions=dimensions,
        dimension_details=dimension_details,
    )

    if cache is not None:
        payload = json.dumps(
            {
                "overall_score": score.overall_score,
                "dimensions": score.dimensions,
                "dimension_details": score.dimension_details,
            }
        )
        await cache.set(cache_key, payload, ttl_seconds=QUALITY_SCORE_CACHE_TTL)

    return score


async def _score_completeness(datahub: DataHubClient, urn: str) -> float:
    """Percentage of schema fields with non-empty descriptions."""
    from datahub.metadata.schema_classes import SchemaMetadataClass

    schema: SchemaMetadataClass | None = await datahub.get_aspect(urn, SchemaMetadataClass)
    if schema is None or not schema.fields:
        return 0.0

    described = sum(1 for f in schema.fields if f.description and f.description.strip())
    return round((described / len(schema.fields)) * 100, 2)


async def _score_freshness(datahub: DataHubClient, urn: str) -> float:
    """Score based on days since last successful operation (0-100)."""
    from datahub.metadata.schema_classes import OperationClass

    operations: list[OperationClass] = await datahub.get_timeseries(urn, OperationClass, limit=1)
    if not operations:
        return 0.0

    last_op = operations[0]
    last_ts = getattr(last_op, "lastUpdatedTimestamp", None) or getattr(
        last_op, "timestampMillis", None
    )
    if last_ts is None:
        return 0.0

    last_dt = datetime.fromtimestamp(last_ts / 1000, tz=UTC)
    days_ago = (datetime.now(tz=UTC) - last_dt).total_seconds() / 86400

    if days_ago <= 1:
        return 100.0
    if days_ago >= 30:
        return 0.0
    return round(max(0.0, 100.0 - (days_ago / 30) * 100), 2)


async def _score_schema_stability(
    datahub: DataHubClient, urn: str
) -> tuple[float, dict[str, Any] | None]:
    """Schema stability based on change frequency over the last 30 days.

    Uses the DataHub Timeline API (getSchemaVersionList) to count schema
    version bumps within the scoring window.  Major version bumps (breaking
    changes) are penalised more heavily than minor bumps.

    Returns (score, details) where details contains major/minor change counts.
    """
    versions = await datahub.get_schema_version_list(urn)

    # No versions at all → dataset has no schema recorded
    if not versions:
        return 0.0, None

    # Only the initial version (0.0.0) → perfectly stable, no changes
    if len(versions) <= 1:
        return 100.0, {"major_changes": 0, "minor_changes": 0}

    cutoff_ms = int(
        (datetime.now(tz=UTC).timestamp() - _SCHEMA_STABILITY_WINDOW_DAYS * 86400) * 1000
    )

    major_changes = 0
    minor_changes = 0
    _semver_re = re.compile(r"^(\d+)\.(\d+)\.\d+")

    prev_major: int | None = None
    prev_minor: int | None = None

    for v in versions:
        ts = v.get("semanticVersionTimestamp")
        semver = v.get("semanticVersion", "")
        m = _semver_re.match(semver)
        if not m:
            continue

        cur_major, cur_minor = int(m.group(1)), int(m.group(2))

        if prev_major is not None and ts is not None and ts >= cutoff_ms:
            if cur_major > prev_major:
                major_changes += 1
            elif cur_minor > prev_minor:
                minor_changes += 1

        prev_major, prev_minor = cur_major, cur_minor

    penalty = major_changes * _MAJOR_CHANGE_PENALTY + minor_changes * _MINOR_CHANGE_PENALTY
    score = round(max(0.0, 100.0 - penalty), 2)

    return score, {"major_changes": major_changes, "minor_changes": minor_changes}


async def _score_data_quality(datahub: DataHubClient, urn: str) -> float:
    """Score from dataset profile — null ratio and row count stability."""
    from datahub.metadata.schema_classes import DatasetProfileClass

    profiles: list[DatasetProfileClass] = await datahub.get_timeseries(
        urn, DatasetProfileClass, limit=5
    )
    if not profiles:
        return 0.0

    latest = profiles[0]
    score = 100.0

    # Penalise for high null ratio across field profiles
    field_profiles = getattr(latest, "fieldProfiles", None) or []
    if field_profiles:
        null_ratios = [
            fp.nullProportion
            for fp in field_profiles
            if hasattr(fp, "nullProportion") and fp.nullProportion is not None
        ]
        if null_ratios:
            avg_null = sum(null_ratios) / len(null_ratios)
            score -= avg_null * 100  # 0% null → 0 penalty; 100% null → -100

    # Penalise if row count is zero
    row_count = getattr(latest, "rowCount", None)
    if row_count is not None and row_count == 0:
        score -= 30.0

    return round(min(max(score, 0.0), 100.0), 2)


async def _score_ownership_tags(datahub: DataHubClient, urn: str) -> float:
    """Has owner + at least 1 tag → 100; partial → 50; nothing → 0."""
    from datahub.metadata.schema_classes import GlobalTagsClass, OwnershipClass

    ownership: OwnershipClass | None = await datahub.get_aspect(urn, OwnershipClass)
    tags: GlobalTagsClass | None = await datahub.get_aspect(urn, GlobalTagsClass)

    has_owner = ownership is not None and bool(getattr(ownership, "owners", []))
    has_tags = tags is not None and bool(getattr(tags, "tags", []))

    if has_owner and has_tags:
        return 100.0
    if has_owner or has_tags:
        return 50.0
    return 0.0
