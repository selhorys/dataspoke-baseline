"""Unit tests for Airflow Pydantic models (airflow/models.py).

Tests:
- DagRunState enum values match Airflow Stable REST API states.
- DagRunResponse round-trip serialization: required fields enforced, optional fields default.
- DagRunResponse.is_terminal is True for success/failed, False for queued/running.
- DagRunResponse.status is an alias for state.

impl interface contract — Airflow REST API contract for state values (queued | running |
success | failed); DagRunResponse field layout, is_terminal, and .status alias are impl
conventions. No dedicated spec section exists in the baseline contract.
"""

import pytest
from pydantic import ValidationError

from src.workflows.airflow.models import DagRunResponse, DagRunState


# ── DagRunState enum ──────────────────────────────────────────────────────────


def test_dag_run_state_values() -> None:
    """DagRunState must have exactly the four Airflow state values.

    Airflow REST API contract: queued | running | success | failed (upstream Airflow).
    impl interface contract — no dedicated spec section in the baseline contract.
    """
    assert DagRunState.queued == "queued"
    assert DagRunState.running == "running"
    assert DagRunState.success == "success"
    assert DagRunState.failed == "failed"


# ── DagRunResponse required fields ────────────────────────────────────────────


def test_dag_run_response_requires_dag_run_id_dag_id_state() -> None:
    """DagRunResponse must require dag_run_id, dag_id, state.

    impl interface contract (Airflow REST API + impl conventions) — required fields from Airflow REST API.
    """
    with pytest.raises(ValidationError):
        DagRunResponse()  # type: ignore[call-arg]  # missing required fields


def test_dag_run_response_valid_construction() -> None:
    """DagRunResponse can be constructed with required fields only.

    impl interface contract (Airflow REST API + impl conventions) — minimal valid payload.
    """
    resp = DagRunResponse(
        dag_run_id="run-2026-05-01",
        dag_id="ingestion-active-daily",
        state=DagRunState.running,
    )
    assert resp.dag_run_id == "run-2026-05-01"
    assert resp.dag_id == "ingestion-active-daily"
    assert resp.state == DagRunState.running


# ── DagRunResponse optional field defaults ────────────────────────────────────


def test_dag_run_response_optional_fields_default_correctly() -> None:
    """DagRunResponse optional fields default: conf={}, logical/start/end_date=None.

    impl interface contract (Airflow REST API + impl conventions) — optional fields from Airflow response.
    """
    resp = DagRunResponse(
        dag_run_id="run-1",
        dag_id="metrics-daily",
        state=DagRunState.queued,
    )
    assert resp.conf == {}
    assert resp.logical_date is None
    assert resp.start_date is None
    assert resp.end_date is None


# ── DagRunResponse.is_terminal ────────────────────────────────────────────────


def test_is_terminal_true_for_success() -> None:
    """DagRunResponse.is_terminal is True when state is success.

    impl interface contract (Airflow REST API + impl conventions) — terminal states: success, failed.
    """
    resp = DagRunResponse(
        dag_run_id="run-1", dag_id="metagen-daily", state=DagRunState.success
    )
    assert resp.is_terminal is True


def test_is_terminal_true_for_failed() -> None:
    """DagRunResponse.is_terminal is True when state is failed.

    impl interface contract (Airflow REST API + impl conventions) — terminal states: success, failed.
    """
    resp = DagRunResponse(
        dag_run_id="run-1", dag_id="ontogen-weekly", state=DagRunState.failed
    )
    assert resp.is_terminal is True


def test_is_terminal_false_for_queued() -> None:
    """DagRunResponse.is_terminal is False when state is queued.

    impl interface contract (Airflow REST API + impl conventions) — non-terminal: queued, running.
    """
    resp = DagRunResponse(
        dag_run_id="run-1", dag_id="metrics-hourly", state=DagRunState.queued
    )
    assert resp.is_terminal is False


def test_is_terminal_false_for_running() -> None:
    """DagRunResponse.is_terminal is False when state is running.

    impl interface contract (Airflow REST API + impl conventions) — non-terminal: queued, running.
    """
    resp = DagRunResponse(
        dag_run_id="run-1", dag_id="ingestion-active-hourly", state=DagRunState.running
    )
    assert resp.is_terminal is False


# ── DagRunResponse.status alias ───────────────────────────────────────────────


def test_status_alias_equals_state() -> None:
    """DagRunResponse.status must equal DagRunResponse.state.

    impl interface contract (Airflow REST API + impl conventions) — .status is a backward-compat alias for .state.
    """
    resp = DagRunResponse(
        dag_run_id="run-1", dag_id="datahub-sync-hourly", state=DagRunState.success
    )
    assert resp.status == resp.state


# ── Round-trip serialization ──────────────────────────────────────────────────


def test_dag_run_response_round_trip() -> None:
    """DagRunResponse serializes and deserializes correctly.

    impl interface contract (Airflow REST API + impl conventions) — model used for API response parsing.
    """
    resp = DagRunResponse(
        dag_run_id="manual__2026-05-01T00:00:00+00:00",
        dag_id="metagen",
        state=DagRunState.success,
        conf={"dataset_urn": "urn:li:dataset:test"},
        logical_date="2026-05-01T00:00:00+00:00",
        start_date="2026-05-01T00:01:00+00:00",
        end_date="2026-05-01T00:02:30+00:00",
    )
    data = resp.model_dump()
    restored = DagRunResponse(**data)

    assert restored.dag_run_id == resp.dag_run_id
    assert restored.state == resp.state
    assert restored.conf == resp.conf
    assert restored.is_terminal is True
