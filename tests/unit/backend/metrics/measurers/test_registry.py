"""Unit tests for the measurer registry.

Tests the spec-mandated contracts of register_measurer, get_measurer, list_measurers:
- register_measurer(name) registers a function under that name.
- get_measurer(name) returns the registered function.
- get_measurer(unknown) returns None.
- list_measurers() returns a sorted list of registered names.
- Re-registering the same name overwrites the previous entry.

spec: feature/BACKEND.md §Metrics Service — baseline measurers are registered
      via the measurer registry; registry look-up drives the measurement dispatch.
"""

import pytest

from src.backend.metrics.measurers.registry import (
    _MEASURERS,
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

    spec: feature/BACKEND.md §Metrics Service — measurer registry maps names to async functions.
    """
    @register_measurer("_test_measurer_alpha")
    async def _alpha(**_):
        return 0.0, {}

    assert get_measurer("_test_measurer_alpha") is _alpha


def test_register_measurer_overwrites_on_duplicate_name() -> None:
    """Re-registering the same name must overwrite the previous entry.

    spec: feature/BACKEND.md §Metrics Service — registry is a dict; last-writer wins.
    """
    @register_measurer("_test_measurer_dup")
    async def _v1(**_):
        return 1.0, {}

    @register_measurer("_test_measurer_dup")
    async def _v2(**_):
        return 2.0, {}

    assert get_measurer("_test_measurer_dup") is _v2, (
        "Second registration must overwrite first (last-writer-wins)."
    )


# ── get_measurer ──────────────────────────────────────────────────────────────


def test_get_measurer_returns_none_for_unknown_name() -> None:
    """get_measurer(unknown) must return None — not raise.

    spec: feature/BACKEND.md §Metrics Service — callers check for None to skip unknown types.
    """
    result = get_measurer("__does_not_exist__")
    assert result is None


def test_get_measurer_returns_registered_callable() -> None:
    """get_measurer returns the function registered via @register_measurer.

    spec: feature/BACKEND.md §Metrics Service — registry look-up drives dispatch.
    """
    @register_measurer("_test_measurer_beta")
    async def _beta(**_):
        return 0.5, {}

    fn = get_measurer("_test_measurer_beta")
    assert callable(fn)
    assert fn is _beta


# ── list_measurers ────────────────────────────────────────────────────────────


def test_list_measurers_returns_sorted_list() -> None:
    """list_measurers() must return names in sorted (lexicographic) order.

    spec: feature/BACKEND.md §Metrics Service — sorted list used for display / iteration.
    """
    @register_measurer("_test_z_measurer")
    async def _z(**_):
        return 0.0, {}

    @register_measurer("_test_a_measurer")
    async def _a(**_):
        return 0.0, {}

    names = list_measurers()
    # filter to test names for isolation
    test_names = [n for n in names if n.startswith("_test_")]
    assert test_names == sorted(test_names), (
        f"list_measurers() must be sorted; got {test_names}"
    )


def test_list_measurers_includes_all_registered() -> None:
    """list_measurers() must include all registered measurer names.

    spec: feature/BACKEND.md §Metrics Service — baseline measurers (pct_fresh,
          pct_rules_passing) must be registered at module import time.
    """
    # The real measurers are registered when their modules are imported.
    import src.backend.metrics.measurers.ingestion_freshness  # noqa: F401
    import src.backend.metrics.measurers.validation_score  # noqa: F401

    names = list_measurers()
    assert "pct_fresh" in names, "pct_fresh must be registered (ingestion_freshness measurer)"
    assert "pct_rules_passing" in names, "pct_rules_passing must be registered (validation_score measurer)"
