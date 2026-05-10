"""Unit tests for Airflow-specific exception classes (airflow/errors.py).

Tests that each exception:
- Is a subclass of DataSpokeError.
- Carries the documented attributes (dag_id, dag_run_id for execution errors;
  dag_id, dag_run_id, timeout_seconds for timeout errors).
- Produces a human-readable message string.

impl interface contract — Airflow REST API contract for DAG run failure and timeout
scenarios; exception class hierarchy and attributes are impl conventions. No dedicated
spec section exists in the baseline contract beyond DataSpokeError hierarchy.
"""

import pytest

from src.workflows.airflow.errors import AirflowExecutionFailedError, AirflowTimeoutError
from src.shared.exceptions import DataSpokeError


# ── AirflowExecutionFailedError ───────────────────────────────────────────────


def test_airflow_execution_failed_is_dataspoke_error() -> None:
    """AirflowExecutionFailedError must be a subclass of DataSpokeError.

    impl interface contract (Airflow REST API + impl conventions) — Airflow errors extend DataSpokeError.
    """
    exc = AirflowExecutionFailedError(
        dag_id="metagen-daily", dag_run_id="run-123"
    )
    assert isinstance(exc, DataSpokeError), (
        f"Expected AirflowExecutionFailedError to be a DataSpokeError subclass; "
        f"got MRO: {[c.__name__ for c in type(exc).__mro__]}"
    )


def test_airflow_execution_failed_carries_dag_id() -> None:
    """AirflowExecutionFailedError.dag_id must equal the constructor argument.

    impl interface contract (Airflow REST API + impl conventions) — dag_id identifies the failed DAG.
    """
    exc = AirflowExecutionFailedError(dag_id="ingestion-active-daily", dag_run_id="run-456")
    assert exc.dag_id == "ingestion-active-daily"


def test_airflow_execution_failed_carries_dag_run_id() -> None:
    """AirflowExecutionFailedError.dag_run_id must equal the constructor argument.

    impl interface contract (Airflow REST API + impl conventions) — dag_run_id identifies the specific run.
    """
    exc = AirflowExecutionFailedError(dag_id="metrics-hourly", dag_run_id="run-789")
    assert exc.dag_run_id == "run-789"


def test_airflow_execution_failed_message_contains_dag_id_and_run_id() -> None:
    """AirflowExecutionFailedError message must include dag_id and dag_run_id.

    impl interface contract (Airflow REST API + impl conventions) — error message is human-readable.
    """
    exc = AirflowExecutionFailedError(
        dag_id="ontogen-weekly", dag_run_id="run-abc", detail="task failed"
    )
    msg = str(exc)
    assert "ontogen-weekly" in msg, f"dag_id not in message: {msg!r}"
    assert "run-abc" in msg, f"dag_run_id not in message: {msg!r}"


def test_airflow_execution_failed_detail_optional() -> None:
    """AirflowExecutionFailedError must work without the optional detail argument.

    impl interface contract (Airflow REST API + impl conventions) — detail is optional context.
    """
    exc = AirflowExecutionFailedError(dag_id="metagen", dag_run_id="run-1")
    # Just must not raise
    assert str(exc)


def test_airflow_execution_failed_detail_included_when_provided() -> None:
    """When detail is provided, it appears in the exception message.

    impl interface contract (Airflow REST API + impl conventions) — detail aids debugging.
    """
    exc = AirflowExecutionFailedError(
        dag_id="datahub-sync-daily", dag_run_id="run-2", detail="upstream error"
    )
    assert "upstream error" in str(exc)


# ── AirflowTimeoutError ───────────────────────────────────────────────────────


def test_airflow_timeout_is_dataspoke_error() -> None:
    """AirflowTimeoutError must be a subclass of DataSpokeError.

    impl interface contract (Airflow REST API + impl conventions) — Airflow errors extend DataSpokeError.
    """
    exc = AirflowTimeoutError(dag_id="metagen", dag_run_id="run-1", timeout_seconds=30.0)
    assert isinstance(exc, DataSpokeError)


def test_airflow_timeout_carries_dag_id() -> None:
    """AirflowTimeoutError.dag_id must equal the constructor argument.

    impl interface contract (Airflow REST API + impl conventions) — dag_id identifies the DAG that timed out.
    """
    exc = AirflowTimeoutError(dag_id="metrics-daily", dag_run_id="run-t", timeout_seconds=25.0)
    assert exc.dag_id == "metrics-daily"


def test_airflow_timeout_carries_dag_run_id() -> None:
    """AirflowTimeoutError.dag_run_id must equal the constructor argument.

    impl interface contract (Airflow REST API + impl conventions) — run_id identifies the timed-out run.
    """
    exc = AirflowTimeoutError(dag_id="metagen-hourly", dag_run_id="run-timeout", timeout_seconds=30.0)
    assert exc.dag_run_id == "run-timeout"


def test_airflow_timeout_message_contains_timeout_and_dag_ids() -> None:
    """AirflowTimeoutError message must include dag_id, dag_run_id, and timeout value.

    impl interface contract (Airflow REST API + impl conventions) — error message is human-readable.
    """
    exc = AirflowTimeoutError(
        dag_id="ingestion-active-weekly",
        dag_run_id="run-999",
        timeout_seconds=45.5,
    )
    msg = str(exc)
    assert "ingestion-active-weekly" in msg
    assert "run-999" in msg
    assert "45.5" in msg or "45" in msg
