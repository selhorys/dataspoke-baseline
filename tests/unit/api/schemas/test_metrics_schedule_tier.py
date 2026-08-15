"""Unit tests for src/api/schemas/metrics.py — schedule_tier, new field set,
dataset_filter caps, and create vs replace contract.

Spec sources:
  spec/API.md §Metric (/spoke/governance/metric):
    - POST /spoke/governance/metric: CreateMetricConfigRequest with metric_id in body
    - PUT /spoke/governance/metric/{id}/attr/conf: ReplaceMetricConfigRequest (replace-only)
    - mode: "active" | "passive"
    - metric_type: "ingestion-freshness" | "validation-score" | "doc-health"
    - metrics: list of {name, color, idx} series descriptors; name from the type's
      emitted keys, name and idx each unique within the metric
    - metric_conf: type-specific (time_window_sec for windowed types; {} for doc-health);
      time_window_sec is "An integer in `[1, 315360000]` (ten years); out of range,
      non-integer, or boolean returns `422 INVALID_PARAMETER`" — the bound itself is
      imported from src.shared.metric_conf so this file cannot drift from it
      (spec/feature/BACKEND.md §Metrics Service §Window bounds)
    - dataset_filter: a SQL WHERE-clause string — ≤ 8,000 chars and ≤ 1,000 string
      literals (spec/API.md §`dataset_filter` grammar — Caps)
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
from src.shared.metric_conf import MAX_TIME_WINDOW_SEC, time_window_sec_error

#: spec/API.md §`dataset_filter` grammar — Caps.
_FILTER_LITERAL_CAP = 1000
_FILTER_CHAR_CAP = 8000

_VALID_INGESTION_BODY = {
    "mode": "active",
    "is_enabled": False,
    "metric_type": "ingestion-freshness",
    "title": "Ingestion freshness",
    "description": "Pct of datasets with a recent successful ingestion run",
    "metrics": [
        {"name": "total", "color": "#64748B", "idx": 1},
        {"name": "ingested_in_time", "color": "#22C55E", "idx": 2},
    ],
    "metric_conf": {"time_window_sec": 86400},
    "dataset_filter": "",
}

_VALID_VALIDATION_BODY = {
    "mode": "active",
    "is_enabled": False,
    "metric_type": "validation-score",
    "title": "Validation score",
    "description": "Sum of validation scores",
    "metrics": [
        {"name": "total", "color": "#64748B", "idx": 1},
        {"name": "validation_score_sum", "color": "#3B82F6", "idx": 2},
    ],
    "metric_conf": {"time_window_sec": 86400},
    "dataset_filter": "",
}

_VALID_DOC_HEALTH_BODY = {
    "mode": "active",
    "is_enabled": False,
    "metric_type": "doc-health",
    "title": "Documentation health",
    "description": "Counts fully documented datasets",
    "metrics": [
        {"name": "total", "color": "#64748B", "idx": 1},
        {"name": "doc_health", "color": "#A855F7", "idx": 2},
    ],
    "metric_conf": {},
    "dataset_filter": "",
}


def _filter_with_literals(count: int) -> str:
    """A syntactically valid filter carrying exactly *count* string literals."""
    return "origin IN (" + ", ".join(f"'v{i}'" for i in range(count)) + ")"


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

    def test_ingestion_freshness_requires_a_time_window(self) -> None:
        """ingestion-freshness with missing time_window_sec raises ValidationError.

        Spec: spec/API.md §Metric — "`ingestion-freshness` and `validation-score` require
              `time_window_sec`".
        """
        with pytest.raises(ValidationError):
            ReplaceMetricConfigRequest(**{**_VALID_INGESTION_BODY, "metric_conf": {}})

    def test_ingestion_freshness_rejects_negative_time_window(self) -> None:
        """ingestion-freshness with time_window_sec = -1 raises ValidationError.

        Spec: spec/API.md §Metric — time_window_sec is "An integer in `[1, 315360000]`
              (ten years); out of range … returns `422 INVALID_PARAMETER`". -1 is below
              the interval.
        """
        with pytest.raises(ValidationError):
            ReplaceMetricConfigRequest(**{
                **_VALID_INGESTION_BODY,
                "metric_conf": {"time_window_sec": -1},
            })

    def test_time_window_of_zero_is_rejected(self) -> None:
        """A zero-length window is below the interval and is rejected.

        Zero is the lower endpoint's immediate neighbour, and unlike -1 it is a value a
        client could plausibly type. A window of zero seconds admits nothing at all: the
        SLO the field declares would be unsatisfiable by construction.

        Spec: spec/feature/BACKEND.md §Metrics Service — Window bounds — "`time_window_sec`
              is an integer in `[1, 315_360_000]` — one second to ten years."
              spec/API.md §Metric — "An integer in `[1, 315360000]` (ten years); out of
              range … returns `422 INVALID_PARAMETER`".
        """
        with pytest.raises(ValidationError) as exc_info:
            ReplaceMetricConfigRequest(**{
                **_VALID_INGESTION_BODY,
                "metric_conf": {"time_window_sec": 0},
            })
        assert time_window_sec_error("ingestion-freshness") in str(exc_info.value), (
            "backstop: rejection must come from the window-bound rule, not from some "
            "other validator on the body."
        )

    def test_time_window_at_the_lower_bound_is_accepted(self) -> None:
        """time_window_sec = 1 is admissible — the interval is closed at 1, not open.

        The mirror of ``test_time_window_of_zero_is_rejected``: the pair fixes the lower
        endpoint at exactly 1, so neither shifting it down to 0 nor up to 2 passes both.

        Spec: spec/feature/BACKEND.md §Metrics Service — Window bounds — "an integer in
              `[1, 315_360_000]` — **one second** to ten years".
              spec/API.md §Metric — "An integer in `[1, 315360000]`".
        """
        req = ReplaceMetricConfigRequest(**{
            **_VALID_INGESTION_BODY,
            "metric_conf": {"time_window_sec": 1},
        })
        assert req.metric_conf == {"time_window_sec": 1}

    def test_time_window_at_the_upper_bound_is_accepted(self) -> None:
        """time_window_sec exactly at the ceiling is admissible.

        Spec: spec/API.md §Metric — time_window_sec is "An integer in `[1, 315360000]`
              (ten years)"; the interval is closed, so the endpoint itself is valid.
              spec/feature/BACKEND.md §Metrics Service §Window bounds states the same
              range.
        """
        req = ReplaceMetricConfigRequest(**{
            **_VALID_INGESTION_BODY,
            "metric_conf": {"time_window_sec": MAX_TIME_WINDOW_SEC},
        })
        assert req.metric_conf == {"time_window_sec": MAX_TIME_WINDOW_SEC}

    def test_time_window_above_the_upper_bound_is_rejected(self) -> None:
        """One second past the ceiling raises ValidationError.

        Spec: spec/API.md §Metric — "An integer in `[1, 315360000]` (ten years); out of
              range … returns `422 INVALID_PARAMETER`". spec/feature/BACKEND.md §Metrics
              Service §Window bounds — "Enforcement lives at the write boundary only —
              the request schema checks create and replace bodies".
        """
        with pytest.raises(ValidationError) as exc_info:
            ReplaceMetricConfigRequest(**{
                **_VALID_INGESTION_BODY,
                "metric_conf": {"time_window_sec": MAX_TIME_WINDOW_SEC + 1},
            })
        assert time_window_sec_error("ingestion-freshness") in str(exc_info.value), (
            "backstop: rejection must come from the window-bound rule, not from some "
            "other validator on the body."
        )

    def test_time_window_boolean_true_is_rejected(self) -> None:
        """A JSON boolean is not a one-second window.

        Spec: spec/feature/BACKEND.md §Metrics Service §Window bounds — "A JSON boolean
              is not an admissible integer here and is rejected with the same error, so
              `{"time_window_sec": true}` is not a one-second window."
              spec/API.md §Metric — "out of range, non-integer, or boolean returns
              `422 INVALID_PARAMETER`".
        """
        with pytest.raises(ValidationError) as exc_info:
            ReplaceMetricConfigRequest(**{
                **_VALID_VALIDATION_BODY,
                "metric_conf": {"time_window_sec": True},
            })
        assert time_window_sec_error("validation-score") in str(exc_info.value), (
            "backstop: rejection must come from the window-bound rule, not from some "
            "other validator on the body."
        )

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
        """A series naming a key the type does not emit raises ValidationError.

        Spec: spec/API.md §Metric — Definition body — "`name` is one of the type's
              emitted keys […]; unknown keys return `422 INVALID_PARAMETER`".
        """
        with pytest.raises(ValidationError):
            ReplaceMetricConfigRequest(**{
                **_VALID_INGESTION_BODY,
                "metrics": [{"name": "nonexistent_key", "color": "#64748B", "idx": 1}],
            })

    def test_metrics_valid_subset_accepted(self) -> None:
        """A subset of the type's emitted keys is accepted.

        Spec: spec/API.md §Metric — Definition body — "`name` is one of the type's
              emitted keys".
        """
        req = ReplaceMetricConfigRequest(**{
            **_VALID_INGESTION_BODY,
            "metrics": [{"name": "total", "color": "#64748B", "idx": 1}],
        })
        assert [s.name for s in req.metrics] == ["total"]

    def test_metrics_duplicate_name_raises(self) -> None:
        """Spec: spec/API.md §Metric — Definition body — "`name` and `idx` are each
        unique within the metric"."""
        with pytest.raises(ValidationError):
            ReplaceMetricConfigRequest(**{
                **_VALID_INGESTION_BODY,
                "metrics": [
                    {"name": "total", "color": "#64748B", "idx": 1},
                    {"name": "total", "color": "#22C55E", "idx": 2},
                ],
            })

    def test_metrics_duplicate_idx_raises(self) -> None:
        """Two series at the same idx have no defined draw order.

        Spec: spec/API.md §Metric — Definition body — "`name` and `idx` are each unique
              within the metric. The dashboard chart draws one line per descriptor, in
              `idx` order".
        """
        with pytest.raises(ValidationError):
            ReplaceMetricConfigRequest(**{
                **_VALID_INGESTION_BODY,
                "metrics": [
                    {"name": "total", "color": "#64748B", "idx": 1},
                    {"name": "ingested_in_time", "color": "#22C55E", "idx": 1},
                ],
            })

    @pytest.mark.parametrize("color", ["slate", "#FFF", "#GGGGGG", "64748B", ""])
    def test_metrics_non_hex_color_raises(self, color: str) -> None:
        """Spec: spec/API.md §Metric — Definition body — "`color` is a `#RRGGBB` hex
        string"."""
        with pytest.raises(ValidationError):
            ReplaceMetricConfigRequest(**{
                **_VALID_INGESTION_BODY,
                "metrics": [{"name": "total", "color": color, "idx": 1}],
            })

    @pytest.mark.parametrize("idx", [0, -1])
    def test_metrics_non_positive_idx_raises(self, idx: int) -> None:
        """Spec: spec/API.md §Metric — Definition body — "`idx` is a positive integer
        display order"."""
        with pytest.raises(ValidationError):
            ReplaceMetricConfigRequest(**{
                **_VALID_INGESTION_BODY,
                "metrics": [{"name": "total", "color": "#64748B", "idx": idx}],
            })

    # ── dataset_filter grammar + caps ─────────────────────────────────────────

    @pytest.mark.parametrize(
        "dataset_filter",
        [
            "",
            "origin = 'PROD'",
            "origin IN ('PROD', 'DEV')",
            "'urn:li:tag:area:catalog' IN tag_urns",
            (
                "origin = 'PROD' AND ('urn:li:tag:area:catalog' IN tag_urns"
                " OR 'urn:li:glossaryTerm:pii.gdpr' IN glossary_term_urns)"
            ),
        ],
    )
    def test_dataset_filter_grammar_forms_accepted(self, dataset_filter: str) -> None:
        """Spec: spec/API.md §`dataset_filter` grammar — the productions and the
        depth-1 parenthesised AND/OR composition its worked example prints."""
        req = ReplaceMetricConfigRequest(**{
            **_VALID_INGESTION_BODY,
            "dataset_filter": dataset_filter,
        })
        assert req.dataset_filter == dataset_filter

    def test_dataset_filter_malformed_raises(self) -> None:
        """Spec: spec/API.md §Error Catalogue — INVALID_DATASET_FILTER, 422, "does not
        parse under the filter grammar, names an unknown column"."""
        from src.shared.dataset_filter import DatasetFilterSyntaxError

        with pytest.raises(DatasetFilterSyntaxError):
            ReplaceMetricConfigRequest(**{
                **_VALID_INGESTION_BODY,
                "dataset_filter": "owner = 'alice'",
            })

    def test_dataset_filter_over_literal_cap_raises(self) -> None:
        """Spec: spec/API.md §`dataset_filter` grammar — Caps: "≤ 1,000 string
        literals"."""
        from src.shared.dataset_filter import DatasetFilterSyntaxError

        with pytest.raises(DatasetFilterSyntaxError):
            ReplaceMetricConfigRequest(**{
                **_VALID_INGESTION_BODY,
                "dataset_filter": _filter_with_literals(_FILTER_LITERAL_CAP + 1),
            })

    def test_dataset_filter_at_literal_cap_accepted(self) -> None:
        """The cap is inclusive — exactly 1,000 literals is admissible.

        Spec: spec/API.md §`dataset_filter` grammar — Caps.
        """
        at_cap = _filter_with_literals(_FILTER_LITERAL_CAP)
        req = ReplaceMetricConfigRequest(**{
            **_VALID_INGESTION_BODY,
            "dataset_filter": at_cap,
        })
        assert req.dataset_filter == at_cap

    def test_dataset_filter_over_character_cap_raises(self) -> None:
        """Spec: spec/API.md §`dataset_filter` grammar — Caps: "filter text ≤ 8,000
        characters"."""
        from src.shared.dataset_filter import DatasetFilterSyntaxError

        prefix = "origin = '"
        over_cap = prefix + "x" * (_FILTER_CHAR_CAP - len(prefix)) + "'"
        assert len(over_cap) == _FILTER_CHAR_CAP + 1
        with pytest.raises(DatasetFilterSyntaxError):
            ReplaceMetricConfigRequest(**{
                **_VALID_INGESTION_BODY,
                "dataset_filter": over_cap,
            })

    def test_dataset_filter_malformed_dataset_urn_literal_raises(self) -> None:
        """Spec: spec/API.md §Error Catalogue — INVALID_DATASET_URN, 422."""
        from src.shared.exceptions import InvalidDatasetUrnError

        with pytest.raises(InvalidDatasetUrnError):
            ReplaceMetricConfigRequest(**{
                **_VALID_INGESTION_BODY,
                "dataset_filter": "dataset_urn = 'not-a-urn'",
            })

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

    def test_dataset_filter_over_literal_cap_raises(self) -> None:
        """Spec: spec/API.md §Error Catalogue — INVALID_DATASET_FILTER is validated on
        `PATCH /spoke/governance/metric/{metric_id}/attr/conf` too."""
        from src.shared.dataset_filter import DatasetFilterSyntaxError

        with pytest.raises(DatasetFilterSyntaxError):
            PatchMetricConfigRequest(
                dataset_filter=_filter_with_literals(_FILTER_LITERAL_CAP + 1)
            )

    def test_dataset_filter_malformed_raises(self) -> None:
        """Spec: spec/API.md §Error Catalogue — INVALID_DATASET_FILTER, 422."""
        from src.shared.dataset_filter import DatasetFilterSyntaxError

        with pytest.raises(DatasetFilterSyntaxError):
            PatchMetricConfigRequest(dataset_filter="owner = 'alice'")

    def test_a_well_formed_filter_patch_is_accepted(self) -> None:
        """Backstop for the two rejections above: a valid PATCH filter is kept verbatim."""
        req = PatchMetricConfigRequest(dataset_filter="origin = 'PROD'")
        assert req.dataset_filter == "origin = 'PROD'"

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
        """CreateMetricConfigRequest inherits the series validation.

        Spec: spec/API.md §Metric — Definition body — unknown `metrics[].name` keys
              return 422 INVALID_PARAMETER; the create body carries the same fields.
        """
        with pytest.raises(ValidationError):
            CreateMetricConfigRequest(
                **{
                    **_VALID_INGESTION_BODY,
                    "metrics": [{"name": "nonexistent_key", "color": "#64748B", "idx": 1}],
                },
                metric_id="my-metric",
            )

    def test_inherited_dataset_filter_cap_fires(self) -> None:
        """The create body validates the filter too — there is no POST on attr/conf.

        Spec: spec/API.md §Error Catalogue — INVALID_DATASET_FILTER is validated on
              "`POST /spoke/governance/metric` (the create body carries the filter —
              there is no `POST` on `attr/conf`)".
        """
        from src.shared.dataset_filter import DatasetFilterSyntaxError

        with pytest.raises(DatasetFilterSyntaxError):
            CreateMetricConfigRequest(
                **{
                    **_VALID_INGESTION_BODY,
                    "dataset_filter": _filter_with_literals(_FILTER_LITERAL_CAP + 1),
                },
                metric_id="my-metric",
            )

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
