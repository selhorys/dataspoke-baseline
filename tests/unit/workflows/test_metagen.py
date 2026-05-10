"""Unit tests for metagen workflow params and DAG registry."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from src.workflows.metagen import MetagenRunParams
from src.workflows.registry import ALL_DAG_IDS

_DAGS_DIR = Path(__file__).parents[3] / "src" / "workflows" / "dags"


# ── MetagenRunParams ──────────────────────────────────────────────────────────


def test_params_requires_dataset_urn() -> None:
    """MetagenRunParams requires dataset_urn."""
    with pytest.raises(ValidationError):
        MetagenRunParams()  # type: ignore[call-arg]


def test_params_defaults() -> None:
    """MetagenRunParams dry_run defaults to False."""
    params = MetagenRunParams(dataset_urn="urn:li:dataset:(urn:li:dataPlatform:postgres,t,PROD)")
    assert params.dry_run is False


def test_params_dry_run_true() -> None:
    """MetagenRunParams accepts dry_run=True."""
    params = MetagenRunParams(
        dataset_urn="urn:li:dataset:(urn:li:dataPlatform:postgres,t,PROD)",
        dry_run=True,
    )
    assert params.dry_run is True


def test_params_stores_dataset_urn() -> None:
    """MetagenRunParams stores dataset_urn correctly."""
    urn = "urn:li:dataset:(urn:li:dataPlatform:hive,mydb.mytable,PROD)"
    params = MetagenRunParams(dataset_urn=urn)
    assert params.dataset_urn == urn


# ── DAG registry ──────────────────────────────────────────────────────────────


def test_metagen_on_demand_dag_in_registry() -> None:
    """The on-demand 'metagen' DAG ID must be in ALL_DAG_IDS."""
    assert "metagen" in ALL_DAG_IDS


def test_metagen_tier_dags_in_registry() -> None:
    """metagen-hourly, metagen-daily, metagen-weekly must be in ALL_DAG_IDS."""
    for tier in ("hourly", "daily", "weekly"):
        assert f"metagen-{tier}" in ALL_DAG_IDS


# ── DAG file content checks ───────────────────────────────────────────────────


@pytest.mark.parametrize("tier", ["hourly", "daily", "weekly"])
def test_metagen_tier_dag_has_correct_dag_id(tier: str) -> None:
    """Each tier DAG file declares the correct dag_id."""
    dag_file = _DAGS_DIR / f"metagen_{tier}.py"
    assert dag_file.exists(), f"Missing dag file: {dag_file}"
    content = dag_file.read_text()
    assert f'"metagen-{tier}"' in content or f"'metagen-{tier}'" in content


def test_metagen_on_demand_dag_references_md5_conf_key_prefix() -> None:
    """On-demand metagen DAG doc mentions 'metagen-' conf-key prefix (for dedup)."""
    dag_file = _DAGS_DIR / "metagen.py"
    assert dag_file.exists()
    content = dag_file.read_text()
    # The doc should mention the metagen-{md5(urn)[:12]} pattern
    assert "metagen-" in content
