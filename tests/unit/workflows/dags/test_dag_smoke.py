"""Smoke tests for all DAG files (except datahub_sync_hourly.py which has its own test).

Tests (parametrized over 14 DAG files):
(a) The file exists and can be read without error.
(b) The DAG file declares a _DAG_ID constant (string literal).
(c) The declared dag_id appears in ALL_DAG_IDS from the registry.

Airflow is not installed in the unit-test environment; tests use Path.read_text()
to inspect the source, following the pattern of test_datahub_sync_daily.py.

spec: feature/BACKEND.md §DAG Catalogue — each DAG file must declare its dag_id
      and that ID must be registered in ALL_DAG_IDS.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.workflows.registry import ALL_DAG_IDS

_DAGS_DIR = Path(__file__).resolve().parents[4] / "src" / "workflows" / "dags"

# All .py files except helpers, __init__, and datahub_sync_hourly (has its own test)
_EXCLUDED = frozenset({"_internal_headers.py", "__init__.py", "datahub_sync_hourly.py"})

_DAG_FILES: list[Path] = [
    f for f in sorted(_DAGS_DIR.glob("*.py"))
    if f.name not in _EXCLUDED
]

# Regex to extract _DAG_ID = "..." or _DAG_ID = '...'
_DAG_ID_RE = re.compile(r'_DAG_ID\s*=\s*["\']([^"\']+)["\']')


@pytest.mark.parametrize("dag_file", _DAG_FILES, ids=[f.stem for f in _DAG_FILES])
def test_dag_file_exists(dag_file: Path) -> None:
    """DAG file must exist in src/workflows/dags/.

    spec: feature/BACKEND.md §DAG Catalogue — every catalogued DAG has a file.
    """
    assert dag_file.is_file(), f"DAG file not found: {dag_file}"


@pytest.mark.parametrize("dag_file", _DAG_FILES, ids=[f.stem for f in _DAG_FILES])
def test_dag_file_declares_dag_id_constant(dag_file: Path) -> None:
    """Each DAG file must declare a _DAG_ID string constant.

    spec: feature/BACKEND.md §DAG Catalogue — dag_id is the stable identifier
          used by Airflow and the registry.
    """
    source = dag_file.read_text(encoding="utf-8")
    match = _DAG_ID_RE.search(source)
    assert match is not None, (
        f"{dag_file.name} must declare '_DAG_ID = \"<dag-id>\"'. "
        "spec: feature/BACKEND.md §DAG Catalogue."
    )


@pytest.mark.parametrize("dag_file", _DAG_FILES, ids=[f.stem for f in _DAG_FILES])
def test_dag_id_is_registered_in_all_dag_ids(dag_file: Path) -> None:
    """The _DAG_ID in each DAG file must appear in ALL_DAG_IDS.

    spec: feature/BACKEND.md §DAG Catalogue — registry is the single source of truth.
    """
    source = dag_file.read_text(encoding="utf-8")
    match = _DAG_ID_RE.search(source)
    if match is None:
        pytest.skip(f"{dag_file.name}: no _DAG_ID found (checked separately by file-exists test)")

    dag_id = match.group(1)
    assert dag_id in ALL_DAG_IDS, (
        f"{dag_file.name} declares dag_id='{dag_id}' which is NOT in ALL_DAG_IDS. "
        f"Add it to src/workflows/registry.py or rename the DAG. "
        f"ALL_DAG_IDS: {sorted(ALL_DAG_IDS)}"
    )


# ── Metrics tier DAGs forward their scheduled boundary time ──────────────────

_METRICS_TIER_DAGS = ["metrics_hourly.py", "metrics_daily.py", "metrics_weekly.py"]


@pytest.mark.parametrize("dag_name", _METRICS_TIER_DAGS)
def test_metrics_tier_dag_sends_scheduled_at_in_its_run_body(dag_name: str) -> None:
    """Each tier DAG puts a `scheduled_at` field in the run request body it builds.

    Without it, a retried or backlogged tier run measures the interval it *executed* in
    rather than the one it is *for*, and the internal route's `scheduled_at` is dead
    weight. All three tiers are checked because the DAG files are near-duplicates, so a
    fix applied to one is easy to forget on the others.

    Source-text check: Airflow is not importable in the unit environment, so the DAG's
    task body cannot be executed or its Jinja rendered — the assertion is that the field
    is present in the constructed body, not what it renders to.

    spec: feature/BACKEND.md §Metrics Service — Measurement instant: for a "Periodic tier
    DAG (`metrics-{hourly,daily,weekly}`)" the instant is "The DAG run's scheduled
    boundary time (Airflow `data_interval_end`), forwarded as `scheduled_at` on the
    internal run request".
    """
    source = (_DAGS_DIR / dag_name).read_text(encoding="utf-8")
    assert '"scheduled_at": scheduled_at' in source, (
        f"{dag_name} must include scheduled_at in the JSON body it builds for "
        "/internal/activities/metrics/run. "
        "spec: feature/BACKEND.md §Metrics Service — Measurement instant."
    )


@pytest.mark.parametrize("dag_name", _METRICS_TIER_DAGS)
def test_metrics_tier_dag_templates_scheduled_at_from_the_dag_run_interval(
    dag_name: str,
) -> None:
    """The forwarded instant is templated from the DAG run's own interval, not from now().

    `data_interval_end` is the scheduled boundary the spec names. `run_after` is the
    fallback for a manually-triggered run, which has no data interval — an unguarded
    `data_interval_end` would render as `None` there and the route would reject the body.
    Both spellings are pinned so neither half can be dropped.

    A rendered-value assertion is out of reach here (no Airflow, no execution context),
    so what is asserted is that the value is a Jinja expression over the DAG run rather
    than a Python-side clock read: a `datetime.now()` in the DAG body would date the run
    at *parse* time, which is a different instant on every scheduler heartbeat.

    spec: feature/BACKEND.md §Metrics Service — Measurement instant, trigger table:
    "The DAG run's scheduled boundary time (Airflow `data_interval_end`), forwarded as
    `scheduled_at` on the internal run request. A manually-triggered run of one of these
    DAGs carries no `data_interval_end`; the DAG falls back to `dag_run.run_after`
    (always present, ≈ the trigger instant) so a manual trigger still renders rather
    than failing at template time".
    """
    source = (_DAGS_DIR / dag_name).read_text(encoding="utf-8")
    assert "dag_run.data_interval_end" in source, (
        f"{dag_name} must template scheduled_at from dag_run.data_interval_end. "
        "spec: feature/BACKEND.md §Metrics Service — Measurement instant."
    )
    assert "dag_run.run_after" in source, (
        f"{dag_name} must fall back to dag_run.run_after for a manual trigger, which "
        "carries no data interval; an unguarded data_interval_end renders as None there."
    )
    assert ".isoformat() }}" in source, (
        f"{dag_name} must forward an RFC 3339 string — the route parses an "
        "AwareDatetime, and a raw pendulum repr is not one."
    )
    assert "datetime.now" not in source, (
        f"{dag_name} must not read a clock in the DAG body: the instant is the DAG run's "
        "own interval boundary, not the moment the file was parsed. "
        "spec: feature/BACKEND.md §Metrics Service — Measurement instant."
    )


def test_dag_file_count_is_exactly_14() -> None:
    """There must be exactly 14 DAG files under test (excluding datahub_sync_hourly).

    spec: feature/BACKEND.md §DAG Catalogue — 15 total = 14 + datahub_sync_hourly.
    """
    assert len(_DAG_FILES) == 14, (
        f"Expected 14 DAG files under test (15 total - 1 for datahub_sync_hourly), "
        f"found {len(_DAG_FILES)}: {[f.name for f in _DAG_FILES]}"
    )
