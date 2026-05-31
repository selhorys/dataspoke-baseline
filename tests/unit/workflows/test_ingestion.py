"""Unit tests for the ingestion workflow module.

The per-dataset get_datasets_for_tier function is removed in the per-source model.
Tier-based dispatch now uses IngestionService.list_active_sources_for_tier().
The ingestion activity at /internal/activities/ingestion/list-active calls that method.

This module tests the IngestionPassiveSyncParams model and any remaining workflow helpers.

Spec: BACKEND.md §Ingestion Workflow, §Tier DAG support
Spec: USE_CASE_en.md §UC1
"""

from __future__ import annotations

import pytest

from src.workflows.ingestion import IngestionPassiveSyncParams


class TestIngestionPassiveSyncParams:
    """Spec: BACKEND.md §Ingestion Workflow — passive-sync activity has no inputs."""

    def test_params_is_instantiable_with_no_args(self) -> None:
        """IngestionPassiveSyncParams can be instantiated with no arguments.

        Spec: BACKEND.md §Ingestion Workflow — passive-sync activity takes no parameters.
        """
        params = IngestionPassiveSyncParams()
        assert params is not None

    def test_params_is_pydantic_model(self) -> None:
        """IngestionPassiveSyncParams is a Pydantic BaseModel for Airflow compatibility."""
        from pydantic import BaseModel
        assert issubclass(IngestionPassiveSyncParams, BaseModel)
