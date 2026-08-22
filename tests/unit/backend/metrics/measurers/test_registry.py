"""Unit tests for the measurer registry.

Tests the spec-mandated contracts of register_measurer, get_measurer, list_measurers:
- register_measurer(name) registers a function under that name.
- get_measurer(name) returns the registered function.
- get_measurer(unknown) returns None.
- list_measurers() returns a sorted list of registered names.
- Re-registering the same name overwrites the previous entry.
- Built-in measurers are registered under 'ingestion-freshness', 'validation-score',
  and 'doc-health' (not old names 'pct_fresh' / 'pct_rules_passing').

Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types — metric_type values
      are 'ingestion-freshness', 'validation-score', 'doc-health'.
Spec: spec/feature/BACKEND.md §Metrics Service §Measurers — one async function per
      built-in metric_type, registered via the measurer registry.
"""

import pytest

from src.backend.metrics.measurers.registry import (
    _MEASURERS,
    MeasurerFn,
    get_measurer,
    list_measurers,
    register_measurer,
)


@pytest.fixture(autouse=True)
def _isolated_registry():
    """Snapshot and restore _MEASURERS so test registrations don't leak."""
    snapshot = dict(_MEASURERS)
    yield
    _MEASURERS.clear()
    _MEASURERS.update(snapshot)


# ── register_measurer ─────────────────────────────────────────────────────────


def test_register_measurer_stores_function_under_name() -> None:
    """register_measurer(name) must store the decorated function retrievable by name.

    Spec: spec/feature/BACKEND.md §Metrics Service — measurer registry maps
          metric-type names to async measurer functions.
    """
    @register_measurer("_test_measurer_alpha")
    async def _alpha(**_):
        return {}, {}

    assert get_measurer("_test_measurer_alpha") is _alpha


def test_register_measurer_overwrites_on_duplicate_name() -> None:
    """Re-registering the same name must overwrite the previous entry.

    Spec: spec/feature/BACKEND.md §Metrics Service — registry is a dict; last-writer wins.
    """
    @register_measurer("_test_measurer_dup")
    async def _v1(**_):
        return {}, {}

    @register_measurer("_test_measurer_dup")
    async def _v2(**_):
        return {}, {}

    assert get_measurer("_test_measurer_dup") is _v2, (
        "Second registration must overwrite first (last-writer-wins)."
    )


# ── get_measurer ──────────────────────────────────────────────────────────────


def test_get_measurer_returns_none_for_unknown_name() -> None:
    """get_measurer(unknown) must return None — not raise.

    Spec: spec/feature/BACKEND.md §Metrics Service — callers check for None
          to skip unknown types.
    """
    result = get_measurer("__does_not_exist__")
    assert result is None


def test_get_measurer_returns_registered_callable() -> None:
    """get_measurer returns the function registered via @register_measurer.

    Spec: spec/feature/BACKEND.md §Metrics Service — registry look-up drives dispatch.
    """
    @register_measurer("_test_measurer_beta")
    async def _beta(**_):
        return {}, {}

    fn = get_measurer("_test_measurer_beta")
    assert callable(fn)
    assert fn is _beta


# ── list_measurers ────────────────────────────────────────────────────────────


def test_list_measurers_returns_registered_names() -> None:
    """list_measurers() must include all registered measurer names.

    Spec: spec/feature/BACKEND.md §Metrics Service — measurer registry maps
          metric-type names to async measurer functions; list_measurers returns
          the registered names. Spec does not mandate a sort order.
    """
    @register_measurer("_test_z_measurer")
    async def _z(**_):
        return {}, {}

    @register_measurer("_test_a_measurer")
    async def _a(**_):
        return {}, {}

    names = list_measurers()
    test_names = set(n for n in names if n.startswith("_test_"))
    assert "_test_z_measurer" in test_names
    assert "_test_a_measurer" in test_names


def test_list_measurers_includes_built_in_types() -> None:
    """list_measurers() must include the three built-in measurer keys.

    Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types — 'ingestion-freshness',
          'validation-score', 'doc-health' are the three built-in types.
    Spec: spec/feature/BACKEND.md §Metrics Service §Measurers — all three are registered
          at module import time.
    """
    import src.backend.metrics.measurers.doc_health  # noqa: F401
    import src.backend.metrics.measurers.ingestion_freshness  # noqa: F401
    import src.backend.metrics.measurers.validation_score  # noqa: F401

    names = list_measurers()
    assert "ingestion-freshness" in names, (
        "'ingestion-freshness' must be registered. "
        "Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types."
    )
    assert "validation-score" in names, (
        "'validation-score' must be registered. "
        "Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types."
    )
    assert "doc-health" in names, (
        "'doc-health' must be registered. "
        "Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types."
    )


def test_registered_keys_match_built_in_types_exactly() -> None:
    """The registered measurer keys equal the spec'd built-in metric_type set.

    Spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types.
    """
    import src.backend.metrics.measurers.doc_health  # noqa: F401
    import src.backend.metrics.measurers.ingestion_freshness  # noqa: F401
    import src.backend.metrics.measurers.validation_score  # noqa: F401

    names = set(list_measurers())
    assert names == {"ingestion-freshness", "validation-score", "doc-health"}


# ── MeasurerFn Protocol ───────────────────────────────────────────────────────


def test_measurer_fn_protocol_signature() -> None:
    """MeasurerFn Protocol has the correct signature shape.

    Spec: spec/feature/BACKEND.md §Metrics Service §Measurers — "Each measurer receives
          the resolved dataset URN list, ``metric_conf``, the run's measurement instant
          (above), a ``DataHubClient``, and an ``AsyncSession``, and returns
          ``(values, verdicts)``".
    """
    import inspect
    sig = inspect.signature(MeasurerFn.__call__)
    params = list(sig.parameters.keys())
    assert "datasets" in params
    assert "metric_conf" in params
    assert "datahub" in params
    assert "db" in params
    assert "now" in params


def test_measurer_fn_protocol_requires_the_measurement_instant() -> None:
    """``now`` is a required keyword-only parameter of every measurer.

    The spec makes the instant the service's reading, "passed to every measurer" — so a
    default here would let a measurer silently fall back to its own wall-clock and
    re-open the divergence the parameter exists to close.

    Spec: spec/feature/BACKEND.md §Metrics Service — Measurement instant: "one clock
          reading per run, computed by the service before it dispatches and passed to
          every measurer, so all of a run's measurers date their evidence against the
          same instant"; §Measurers: "The instant is a parameter rather than a
          per-measurer clock read so that one run cannot date two measurers differently".
    """
    import inspect

    parameter = inspect.signature(MeasurerFn.__call__).parameters["now"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY, (
        "`now` travels with `datahub`/`db` as a keyword-only argument. "
        "Spec: spec/feature/BACKEND.md §Metrics Service §Measurers."
    )
    assert parameter.default is inspect.Parameter.empty, (
        "`now` carries no default: a measurer must be handed the run's instant, never "
        "fall back to its own clock. "
        "Spec: spec/feature/BACKEND.md §Metrics Service — Measurement instant."
    )


def test_every_built_in_measurer_accepts_the_measurement_instant() -> None:
    """All three registered measurers take ``now`` — signature uniformity, not opt-in.

    ``doc-health`` dates nothing against the instant — "a documentation state carries no
    timestamp" — and still declares it: the spec's parameter list is unconditional, not
    per-measurer. Without this test a measurer could drop the parameter and only its own
    file would notice.

    Spec: spec/feature/BACKEND.md §Metrics Service §Measurers — "**Each** measurer
          receives the resolved dataset URN list, ``metric_conf``, the run's measurement
          instant (above), a ``DataHubClient``, and an ``AsyncSession``" (emphasis
          added); §Verdict contract — ``evidence_at`` is "``None``, since a documentation
          state carries no timestamp".
    """
    import inspect

    import src.backend.metrics.measurers.doc_health  # noqa: F401
    import src.backend.metrics.measurers.ingestion_freshness  # noqa: F401
    import src.backend.metrics.measurers.validation_score  # noqa: F401

    for metric_type in ("ingestion-freshness", "validation-score", "doc-health"):
        fn = get_measurer(metric_type)
        assert fn is not None, f"{metric_type} must be registered"
        parameter = inspect.signature(fn).parameters["now"]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY, metric_type
        assert parameter.default is inspect.Parameter.empty, metric_type
