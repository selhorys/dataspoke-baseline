"""Unit tests for ontogen workflow params and DAG registry."""

import importlib.util
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.workflows.ontogen import OntogenRunParams
from src.workflows.registry import ALL_DAG_IDS

_DAGS_DIR = Path(__file__).parents[3] / "src" / "workflows" / "dags"


# ── OntogenRunParams ──────────────────────────────────────────────────────────


def test_params_defaults() -> None:
    """OntogenRunParams has sensible defaults."""
    params = OntogenRunParams()
    assert params.tier is None
    assert params.dry_run is False
    assert params.prompt_md is None


def test_params_with_tier() -> None:
    """OntogenRunParams accepts a valid schedule tier."""
    params = OntogenRunParams(tier="hourly", dry_run=False)
    assert params.tier == "hourly"


def test_params_dry_run_true() -> None:
    """OntogenRunParams accepts dry_run=True."""
    params = OntogenRunParams(dry_run=True)
    assert params.dry_run is True


def test_params_with_prompt_md() -> None:
    """OntogenRunParams accepts an optional prompt_md string."""
    params = OntogenRunParams(prompt_md="# Custom prompt")
    assert params.prompt_md == "# Custom prompt"


def test_params_rejects_wrong_type_for_dry_run() -> None:
    """OntogenRunParams rejects a dict value for dry_run with ValidationError.

    Pydantic v2 coerces scalar-compatible types (str/int) but a dict literal
    is not bool-coercible and must raise ValidationError.
    """
    from pydantic import ValidationError as PydanticValidationError
    with pytest.raises(PydanticValidationError):
        OntogenRunParams(dry_run={"x": 1})  # type: ignore[arg-type]


# ── DAG registry ──────────────────────────────────────────────────────────────


def test_ontogen_on_demand_dag_in_registry() -> None:
    """The on-demand 'ontogen' DAG ID must be in ALL_DAG_IDS."""
    assert "ontogen" in ALL_DAG_IDS


def test_ontogen_tier_dags_in_registry() -> None:
    """ontogen-hourly, ontogen-daily, ontogen-weekly must all be in ALL_DAG_IDS."""
    for tier in ("hourly", "daily", "weekly"):
        assert f"ontogen-{tier}" in ALL_DAG_IDS


# ── DAG file content checks ───────────────────────────────────────────────────


@pytest.mark.parametrize("tier", ["hourly", "daily", "weekly"])
def test_ontogen_tier_dag_has_correct_dag_id(tier: str) -> None:
    """Each tier DAG file declares the correct dag_id."""
    dag_file = _DAGS_DIR / f"ontogen_{tier}.py"
    assert dag_file.exists(), f"Missing dag file: {dag_file}"
    content = dag_file.read_text()
    assert f'"ontogen-{tier}"' in content or f"'ontogen-{tier}'" in content


def test_ontogen_on_demand_dag_references_singleton_conf_key() -> None:
    """On-demand ontogen DAG doc mentions 'ontogen-singleton' for dedup."""
    dag_file = _DAGS_DIR / "ontogen.py"
    assert dag_file.exists()
    content = dag_file.read_text()
    assert "ontogen-singleton" in content
