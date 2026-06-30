"""Unit tests for src/api/schemas/metrics.py — schedule_tier, new field set,
dataset_filter caps, and create vs replace contract.

Spec sources:
  spec/API.md §Metric (/spoke/governance/metric):
    - POST /spoke/governance/metric: CreateMetricConfigRequest with metric_id in body
    - PUT /spoke/governance/metric/{id}/attr/conf: ReplaceMetricConfigRequest (replace-only)
    - mode: "active" | "passive"
    - metric_type: "ingestion-freshness" | "validation-score" | "doc-health"
    - metrics: list[str] subset of type's emitted keys
    - metric_conf: type-specific (time_window_sec for windowed types; {} for doc-health)
    - dataset_filter: {origin, tags, glossary_terms, dataset_urns} — each list capped at 1,000
    - schedule_tier: "hourly" | "daily" | "weekly" | null
  spec/feature/BACKEND_SCHEMA.md §metric_definitions — column shapes.
  spec/USE_CASE_en.md §UC5 §Built-in active metric types — emitted keys per type.
  spec/API.md §Governance — Metric (Definition body) — metric_id in body,
    kebab pattern, 422 on bad format, 409 METRIC_EXISTS on collision.
"""

import pytest
from pydantic import ValidationError

from src.api.schemas.metrics import (
    CreateMetricConfigRequest,
    MetricDefinitionListItem,
    MetricDefinitionResponse,
    PatchMetricConfigRequest,
    ReplaceMetricConfigRequest,
)

_DATASET_FILTER_LIST_CAP = 1000

_VALID_INGESTION_BODY = {
    "mode": "active",
    "is_enabled": False,
    "metric_type": "ingestion-freshness",
    "title": "Ingestion freshness",
    "description": "Pct of datasets with a recent successful ingestion run",
    "metrics": ["total", "ingested_in_time"],
    "metric_conf": {"time_window_sec": 86400},
    "dataset_filter": {},
}

_VALID_VALIDATION_BODY = {
    "mode": "active",
    "is_enabled": False,
    "metric_type": "validation-score",
    "title": "Validation score",
    "description": "Sum of validation scores",
    "metrics": ["total", "validation_score_sum"],
    "metric_conf": {"time_window_sec": 86400},
    "dataset_filter": {},
}

_VALID_DOC_HEALTH_BODY = {
    "mode": "active",
    "is_enabled": False,
    "metric_type": "doc-health",
    "title": "Documentation health",
    "description": "Counts fully documented datasets",
    "metrics": ["total", "doc_health"],
    "metric_conf": {},
    "dataset_filter": {},
}


def _too_many(dimension: str) -> list[str]:
    if dimension == "dataset_urns":
        return [
            f"urn:li:dataset:(urn:li:dataPlatform:postgres,db.t{i},PROD)"
            for i in range(_DATASET_FILTER_LIST_CAP + 1)
        ]
    prefix = "urn:li:tag:t" if dimension == "tags" else "urn:li:glossaryTerm:t"
    return [f"{prefix}{i}" for i in range(_DATASET_FILTER_LIST_CAP + 1)]


class TestReplaceMetricConfigRequest:
    # ── schedule_tier ─────────────────────────────────────────────────────────

    def test_schedule_tier_accepts_valid_values(self) -> None:
        """schedule_tier accepts hourly, daily, weekly, and None.

        Spec: spec/feature/BACKEND_SCHEMA.md §metric_definitions — schedule_tier
              is 'hourly', 'daily', 'weekly', or null.
        """
        for tier in ("hourly", "daily", "weekly", None):
            req = ReplaceMetricConfigRequest(**_VALID_INGESTION_BODY, schedule_tier=tier)
            assert req.schedule_tier == tier

    def test_schedule_tier_rejects_invalid_value(self) -> None:
        """schedule_tier='minutely' raises ValidationError.

        Spec: spec/API.md §Metric — schedule_tier is a Literal type.
        """
        with pytest.raises(ValidationError):
            ReplaceMetricConfigRequest(**_VALID_INGESTION_BODY, schedule_tier="minutely")  # type: ignore[arg-type]

    # ── mode field ────────────────────────────────────────────────────────────

    def test_mode_active_accepted(self) -> None:
        """mode='active' is accepted.

        Spec: spec/API.md §Metric — mode is 'active' or 'passive'.
        """
        req = ReplaceMetricConfigRequest(**{**_VALID_INGESTION_BODY, "mode": "active"})
        assert req.mode == "active"

    def test_mode_passive_accepted_at_schema_layer(self) -> None:
        """mode='passive' is accepted at the schema layer (501 is raised at the route).

        Spec: spec/API.md §Metric — 'passive' is reserved; PUT with mode:'passive'
              returns 501 NOT_IMPLEMENTED. This is enforced at the route handler,
              not the Pydantic schema.
        """
        req = ReplaceMetricConfigRequest(**{**_VALID_INGESTION_BODY, "mode": "passive"})
        assert req.mode == "passive"

    def test_mode_invalid_rejected(self) -> None:
        """Unknown mode value raises ValidationError.

        Spec: spec/API.md §Metric — mode is Literal["active", "passive"].
        """
        with pytest.raises(ValidationError):
            ReplaceMetricConfigRequest(**{**_VALID_INGESTION_BODY, "mode": "unknown"})  # type: ignore[arg-type]

    # ── metric_type field ─────────────────────────────────────────────────────

    def test_metric_type_valid_values_accepted(self) -> None:
        """Three valid metric_type values are accepted.

        Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types.
        """
        for body in (_VALID_INGESTION_BODY, _VALID_VALIDATION_BODY, _VALID_DOC_HEALTH_BODY):
            req = ReplaceMetricConfigRequest(**body)
            assert req.metric_type in ("ingestion-freshness", "validation-score", "doc-health")

    def test_metric_type_invalid_rejected(self) -> None:
        """Unknown metric_type raises ValidationError.

        Spec: spec/API.md §Metric — metric_type is Literal; unsupported values return
              422 INVALID_PARAMETER.
        """
        with pytest.raises(ValidationError):
            ReplaceMetricConfigRequest(**{**_VALID_INGESTION_BODY, "metric_type": "unknown-type"})

    # ── metric_conf validation ────────────────────────────────────────────────

    def test_ingestion_freshness_requires_positive_time_window(self) -> None:
        """ingestion-freshness with missing time_window_sec raises ValidationError.

        Spec: spec/API.md §Metric — metric_conf must contain time_window_sec
              (positive int) for ingestion-freshness.
        """
        with pytest.raises(ValidationError):
            ReplaceMetricConfigRequest(**{**_VALID_INGESTION_BODY, "metric_conf": {}})

    def test_ingestion_freshness_rejects_negative_time_window(self) -> None:
        """ingestion-freshness with time_window_sec <= 0 raises ValidationError.

        Spec: spec/API.md §Metric — time_window_sec must be positive int.
        """
        with pytest.raises(ValidationError):
            ReplaceMetricConfigRequest(**{
                **_VALID_INGESTION_BODY,
                "metric_conf": {"time_window_sec": -1},
            })

    def test_validation_score_requires_positive_time_window(self) -> None:
        """validation-score with missing time_window_sec raises ValidationError.

        Spec: spec/API.md §Metric — time_window_sec required for validation-score.
        """
        with pytest.raises(ValidationError):
            ReplaceMetricConfigRequest(**{**_VALID_VALIDATION_BODY, "metric_conf": {}})

    def test_doc_health_rejects_nonempty_metric_conf(self) -> None:
        """doc-health with non-empty metric_conf raises ValidationError.

        Spec: spec/API.md §Metric — metric_conf must be {} for doc-health.
        """
        with pytest.raises(ValidationError):
            ReplaceMetricConfigRequest(**{
                **_VALID_DOC_HEALTH_BODY,
                "metric_conf": {"time_window_sec": 86400},
            })

    def test_doc_health_empty_metric_conf_accepted(self) -> None:
        """doc-health with metric_conf={} is accepted.

        Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types — doc-health
              metric_conf is {}.
        """
        req = ReplaceMetricConfigRequest(**_VALID_DOC_HEALTH_BODY)
        assert req.metric_conf == {}

    # ── metrics[] validation ──────────────────────────────────────────────────

    def test_metrics_unknown_key_raises(self) -> None:
        """metrics[] containing a key not emitted by the type raises ValidationError.

        Spec: spec/API.md §Metric — unknown keys in metrics[] return
              422 INVALID_PARAMETER.
        """
        with pytest.raises(ValidationError):
            ReplaceMetricConfigRequest(**{
                **_VALID_INGESTION_BODY,
                "metrics": ["nonexistent_key"],
            })

    def test_metrics_valid_subset_accepted(self) -> None:
        """metrics[] containing a valid subset of emitted keys is accepted.

        Spec: spec/API.md §Metric — metrics[] must be a subset of the type's
              emitted keys.
        """
        req = ReplaceMetricConfigRequest(**{**_VALID_INGESTION_BODY, "metrics": ["total"]})
        assert req.metrics == ["total"]

    # ── dataset_filter caps ───────────────────────────────────────────────────

    @pytest.mark.parametrize("dimension", ["dataset_urns", "tags", "glossary_terms"])
    def test_dataset_filter_dimension_exceeds_cap_raises(self, dimension: str) -> None:
        """dataset_filter.{dimension} > 1000 raises ValidationError.

        Spec: spec/API.md §Metric — dataset_filter list dimensions capped at 1,000.
        """
        body = {
            **_VALID_INGESTION_BODY,
            "dataset_filter": {dimension: _too_many(dimension)},
        }
        with pytest.raises(ValidationError):
            ReplaceMetricConfigRequest(**body)

    def test_dataset_filter_at_cap_accepted(self) -> None:
        """dataset_filter.dataset_urns with exactly 1,000 entries is accepted.

        Spec: spec/API.md §Metric — list capped at 1,000; exactly 1,000 is allowed.
        """
        exactly_cap = [
            f"urn:li:dataset:(urn:li:dataPlatform:postgres,db.t{i},PROD)"
            for i in range(_DATASET_FILTER_LIST_CAP)
        ]
        req = ReplaceMetricConfigRequest(**{
            **_VALID_INGESTION_BODY,
            "dataset_filter": {"dataset_urns": exactly_cap},
        })
        assert len(req.dataset_filter["dataset_urns"]) == _DATASET_FILTER_LIST_CAP

    # ── Field set matches spec ────────────────────────────────────────────────

    def test_field_set_matches_spec(self) -> None:
        """ReplaceMetricConfigRequest exposes exactly the spec'd field set.

        Spec: spec/feature/BACKEND_SCHEMA.md §metric_definitions; spec/API.md
              §Metric — PUT/PATCH .../attr/conf body.
        """
        ReplaceMetricConfigRequest(**_VALID_INGESTION_BODY)  # smoke-validate
        actual = set(ReplaceMetricConfigRequest.model_fields.keys())
        expected = {
            "mode",
            "is_enabled",
            "metric_type",
            "title",
            "description",
            "metrics",
            "metric_conf",
            "schedule_tier",
            "dataset_filter",
        }
        assert actual == expected


class TestPatchMetricConfigRequest:
    # ── schedule_tier ─────────────────────────────────────────────────────────

    def test_schedule_tier_accepts_valid_values(self) -> None:
        """schedule_tier accepts hourly, daily, weekly, and None.

        Spec: spec/feature/BACKEND_SCHEMA.md §metric_definitions.
        """
        for tier in ("hourly", "daily", "weekly", None):
            req = PatchMetricConfigRequest(schedule_tier=tier)
            assert req.schedule_tier == tier

    def test_schedule_tier_rejects_invalid_value(self) -> None:
        """schedule_tier='yearly' raises ValidationError.

        Spec: spec/API.md §Metric — schedule_tier is a Literal type.
        """
        with pytest.raises(ValidationError):
            PatchMetricConfigRequest(schedule_tier="yearly")  # type: ignore[arg-type]

    # ── dataset_filter caps ───────────────────────────────────────────────────

    @pytest.mark.parametrize("dimension", ["dataset_urns", "tags", "glossary_terms"])
    def test_dataset_filter_dimension_exceeds_cap_raises(self, dimension: str) -> None:
        """PATCH with dataset_filter.{dimension} > 1000 raises.

        Spec: spec/API.md §Metric — dataset_filter list dimensions capped at 1,000.
        """
        with pytest.raises(ValidationError):
            PatchMetricConfigRequest(dataset_filter={dimension: _too_many(dimension)})

    # ── Partial update — all fields optional ─────────────────────────────────

    def test_is_enabled_only_patch_is_valid(self) -> None:
        """PATCH with only is_enabled is valid.

        Spec: spec/API.md §Metric — PATCH updates metric definition fields (partial).
        """
        req = PatchMetricConfigRequest(is_enabled=True)
        assert req.is_enabled is True
        assert req.mode is None
        assert req.metric_type is None

    def test_empty_patch_is_valid(self) -> None:
        """Empty PATCH body is valid (all fields are optional).

        Spec: spec/API.md §Metric — PATCH is partial update.
        """
        req = PatchMetricConfigRequest()
        assert req.is_enabled is None
        assert req.mode is None


class TestCreateMetricConfigRequest:
    """Tests for CreateMetricConfigRequest — POST /spoke/governance/metric body.

    Spec: spec/USE_CASE_en.md §UC5 §API Mapping — POST /spoke/governance/metric; metric_id
          is supplied in the request body. Bad-format metric_id → 422. Collision → 409.
    Spec: spec/API.md §Governance — Metric (Definition body) —
          metric_id kebab pattern: ^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$|^[a-z0-9]$.
    """

    # ── metric_id format validation ───────────────────────────────────────────

    def test_valid_metric_id_accepted(self) -> None:
        """CreateMetricConfigRequest accepts a well-formed kebab metric_id.

        Spec: spec/API.md §Governance — Metric (Definition body) —
              metric_id kebab pattern allows lowercase alphanumeric and hyphens,
              must start and end with an alnum character.
        """
        req = CreateMetricConfigRequest(
            **_VALID_INGESTION_BODY,
            metric_id="my-custom-metric",
        )
        assert req.metric_id == "my-custom-metric"

    def test_single_char_metric_id_accepted(self) -> None:
        """Single lowercase alphanumeric character is a valid metric_id.

        Spec: spec/API.md §Governance — Metric (Definition body) —
              pattern ^[a-z0-9]$ matches single-char ids.
        """
        req = CreateMetricConfigRequest(
            **_VALID_INGESTION_BODY,
            metric_id="a",
        )
        assert req.metric_id == "a"

    def test_metric_id_with_numbers_accepted(self) -> None:
        """metric_id containing digits is accepted.

        Spec: spec/API.md §Governance — Metric (Definition body) —
              pattern allows [a-z0-9] characters.
        """
        req = CreateMetricConfigRequest(
            **_VALID_INGESTION_BODY,
            metric_id="metric-v2",
        )
        assert req.metric_id == "metric-v2"

    def test_uppercase_metric_id_rejected(self) -> None:
        """metric_id with uppercase letters raises ValidationError (422-equivalent).

        Spec: spec/API.md §Governance — Metric (Definition body) —
              metric_id must be lowercase kebab; uppercase is rejected with 422.
        """
        with pytest.raises(ValidationError):
            CreateMetricConfigRequest(
                **_VALID_INGESTION_BODY,
                metric_id="MyMetric",
            )

    def test_metric_id_with_underscore_rejected(self) -> None:
        """metric_id containing underscores raises ValidationError.

        Spec: spec/API.md §Governance — Metric (Definition body) —
              metric_id pattern only allows hyphens as separators, not underscores.
        """
        with pytest.raises(ValidationError):
            CreateMetricConfigRequest(
                **_VALID_INGESTION_BODY,
                metric_id="with_underscore",
            )

    def test_metric_id_with_leading_hyphen_rejected(self) -> None:
        """metric_id starting with a hyphen raises ValidationError.

        Spec: spec/API.md §Governance — Metric (Definition body) —
              metric_id must start with [a-z0-9]; a leading hyphen is rejected.
        """
        with pytest.raises(ValidationError):
            CreateMetricConfigRequest(
                **_VALID_INGESTION_BODY,
                metric_id="-leading",
            )

    def test_metric_id_with_trailing_hyphen_rejected(self) -> None:
        """metric_id ending with a hyphen raises ValidationError.

        Spec: spec/API.md §Governance — Metric (Definition body) —
              metric_id must end with [a-z0-9]; a trailing hyphen is rejected.
        """
        with pytest.raises(ValidationError):
            CreateMetricConfigRequest(
                **_VALID_INGESTION_BODY,
                metric_id="trailing-",
            )

    def test_metric_id_with_space_rejected(self) -> None:
        """metric_id containing a space raises ValidationError.

        Spec: spec/API.md §Governance — Metric (Definition body) —
              metric_id is a strict kebab slug; spaces are not allowed.
        """
        with pytest.raises(ValidationError):
            CreateMetricConfigRequest(
                **_VALID_INGESTION_BODY,
                metric_id="with space",
            )

    # ── Inherited validators fire ─────────────────────────────────────────────

    def test_inherited_metric_conf_validator_fires(self) -> None:
        """CreateMetricConfigRequest inherits metric_conf validation from the replace body.

        Spec: spec/API.md §Metric — ingestion-freshness requires time_window_sec;
              CreateMetricConfigRequest must apply the same rules as ReplaceMetricConfigRequest.
        """
        with pytest.raises(ValidationError):
            CreateMetricConfigRequest(
                **{**_VALID_INGESTION_BODY, "metric_conf": {}},
                metric_id="my-metric",
            )

    def test_inherited_metrics_subset_validator_fires(self) -> None:
        """CreateMetricConfigRequest inherits metrics[] subset validation.

        Spec: spec/API.md §Metric — unknown metrics[] keys return 422 INVALID_PARAMETER.
              This check applies on create as well as replace.
        """
        with pytest.raises(ValidationError):
            CreateMetricConfigRequest(
                **{**_VALID_INGESTION_BODY, "metrics": ["nonexistent_key"]},
                metric_id="my-metric",
            )

    def test_inherited_dataset_filter_cap_fires(self) -> None:
        """CreateMetricConfigRequest inherits dataset_filter cap validation.

        Spec: spec/API.md §Metric — dataset_filter list dimensions capped at 1,000;
              same rule applies to both POST create and PUT replace.
        """
        over_cap = [
            f"urn:li:dataset:(urn:li:dataPlatform:postgres,db.t{i},PROD)"
            for i in range(_DATASET_FILTER_LIST_CAP + 1)
        ]
        body = {
            **_VALID_INGESTION_BODY,
            "metric_id": "my-metric",
            "dataset_filter": {"dataset_urns": over_cap},
        }
        with pytest.raises(ValidationError):
            CreateMetricConfigRequest(**body)

    # ── Field set ─────────────────────────────────────────────────────────────

    def test_field_set_includes_metric_id(self) -> None:
        """CreateMetricConfigRequest has metric_id plus all replace-body fields.

        Spec: spec/USE_CASE_en.md §UC5 §API Mapping — POST /spoke/governance/metric accepts
              metric_id in the request body alongside the definition fields.
        """
        actual = set(CreateMetricConfigRequest.model_fields.keys())
        replace_fields = set(ReplaceMetricConfigRequest.model_fields.keys())
        assert "metric_id" in actual
        assert replace_fields.issubset(actual), (
            "CreateMetricConfigRequest must include all ReplaceMetricConfigRequest fields."
        )
        assert actual == replace_fields | {"metric_id"}


class TestLastRunAtExposure:
    """``last_run_at`` is a list-row-only field.

    Spec: spec/API.md §Metric — GET /spoke/governance/metric — each list row
          carries last_run_at; single-GET / attr/conf / create / replace / patch
          use the bare definition response and do NOT expose it.
    Spec: spec/feature/BACKEND.md §Metrics Service — last_run_at is a
          list-row-only field.
    """

    def test_list_item_exposes_last_run_at(self) -> None:
        """MetricDefinitionListItem (list-row schema) carries last_run_at."""
        assert "last_run_at" in MetricDefinitionListItem.model_fields, (
            "GET /spoke/governance/metric list rows must carry last_run_at. "
            "Spec: spec/API.md §Metric — list-row last_run_at."
        )

    def test_definition_response_omits_last_run_at(self) -> None:
        """The bare MetricDefinitionResponse (single-GET / conf / create / replace /
        patch) must NOT expose last_run_at."""
        assert "last_run_at" not in MetricDefinitionResponse.model_fields, (
            "single-GET / attr/conf / create / replace / patch responses use the "
            "bare MetricDefinitionResponse and must not expose last_run_at. "
            "Spec: spec/feature/BACKEND.md §Metrics Service — list-row-only field."
        )

    def test_list_item_is_superset_of_bare_response(self) -> None:
        """The list-row schema is the bare response plus exactly last_run_at."""
        bare = set(MetricDefinitionResponse.model_fields.keys())
        list_row = set(MetricDefinitionListItem.model_fields.keys())
        assert list_row == bare | {"last_run_at"}, (
            "MetricDefinitionListItem must be MetricDefinitionResponse + last_run_at. "
            "Spec: spec/API.md §Metric — list adds only last_run_at."
        )
