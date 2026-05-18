"""Unit tests for metagen workflow params and DAG registry.

spec: feature/BACKEND.md §DAG Catalogue — metagen tier DAGs.
spec: feature/BACKEND.md §Concurrency Guards — singleton lock per run.
spec: API.md §Trigger — MetagenRunParams accepts optional dataset_urns and dry_run.
"""

from pathlib import Path

import pytest

from src.workflows.metagen import MetagenRunParams
from src.workflows.registry import ALL_DAG_IDS

_DAGS_DIR = Path(__file__).parents[3] / "src" / "workflows" / "dags"


# ── MetagenRunParams ──────────────────────────────────────────────────────────


def test_params_defaults_are_none_and_false() -> None:
    """MetagenRunParams can be instantiated with no arguments; both fields default.

    spec: feature/BACKEND.md §DAG Catalogue — dataset_urns is optional (None = all
    in-scope from global conf); dry_run defaults to False.
    """
    params = MetagenRunParams()
    assert params.dataset_urns is None
    assert params.dry_run is False


def test_params_accepts_dataset_urns_list() -> None:
    """MetagenRunParams stores an explicit dataset_urns list.

    spec: feature/BACKEND.md §Metadata Generation Service — run() accepts optional
    dataset_urns override to scope the run to specific datasets.
    """
    urns = [
        "urn:li:dataset:(urn:li:dataPlatform:postgres,t,PROD)",
        "urn:li:dataset:(urn:li:dataPlatform:hive,s.t,PROD)",
    ]
    params = MetagenRunParams(dataset_urns=urns)
    assert params.dataset_urns == urns
    assert params.dry_run is False


def test_params_accepts_dry_run_true() -> None:
    """MetagenRunParams accepts dry_run=True alongside optional dataset_urns.

    spec: feature/BACKEND.md §Metadata Generation Service — dry_run omits persistence.
    """
    params = MetagenRunParams(dry_run=True)
    assert params.dry_run is True
    assert params.dataset_urns is None


def test_params_accepts_both_dataset_urns_and_dry_run() -> None:
    """MetagenRunParams accepts both dataset_urns and dry_run together.

    spec: feature/BACKEND.md §Metadata Generation Service — the two fields are
    orthogonal (scope + mode).
    """
    urns = ["urn:li:dataset:(urn:li:dataPlatform:postgres,a,PROD)"]
    params = MetagenRunParams(dataset_urns=urns, dry_run=True)
    assert params.dataset_urns == urns
    assert params.dry_run is True


def test_params_has_no_singular_dataset_urn_field() -> None:
    """MetagenRunParams does not expose a singular dataset_urn field.

    spec: feature/BACKEND.md §DAG Catalogue — the singleton pipeline operates
    globally; no per-dataset trigger surface on this model.
    """
    params = MetagenRunParams()
    assert not hasattr(params, "dataset_urn"), (
        "singular dataset_urn field must not exist on MetagenRunParams"
    )


# ── DAG registry ──────────────────────────────────────────────────────────────


def test_metagen_tier_dags_in_registry() -> None:
    """metagen-hourly, metagen-daily, metagen-weekly must be in ALL_DAG_IDS.

    spec: feature/BACKEND.md §DAG Catalogue — three fixed schedule tiers.
    """
    for tier in ("hourly", "daily", "weekly"):
        assert f"metagen-{tier}" in ALL_DAG_IDS


# ── DAG file content checks ───────────────────────────────────────────────────


@pytest.mark.parametrize("tier", ["hourly", "daily", "weekly"])
def test_metagen_tier_dag_has_correct_dag_id(tier: str) -> None:
    """Each tier DAG file declares the correct dag_id string.

    spec: feature/BACKEND.md §DAG Catalogue — dag_id convention: 'metagen-{tier}'.
    """
    dag_file = _DAGS_DIR / f"metagen_{tier}.py"
    assert dag_file.exists(), f"Missing dag file: {dag_file}"
    content = dag_file.read_text()
    assert f'"metagen-{tier}"' in content or f"'metagen-{tier}'" in content
