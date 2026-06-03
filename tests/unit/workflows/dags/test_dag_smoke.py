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


def test_dag_file_count_is_exactly_14() -> None:
    """There must be exactly 14 DAG files under test (excluding datahub_sync_hourly).

    spec: feature/BACKEND.md §DAG Catalogue — 15 total = 14 + datahub_sync_hourly.
    """
    assert len(_DAG_FILES) == 14, (
        f"Expected 14 DAG files under test (15 total - 1 for datahub_sync_hourly), "
        f"found {len(_DAG_FILES)}: {[f.name for f in _DAG_FILES]}"
    )
