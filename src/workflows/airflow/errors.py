"""Airflow-specific error types."""

from src.shared.exceptions import DataSpokeError


class AirflowExecutionFailedError(DataSpokeError):
    """Raised when an Airflow DAG run completes with failed state."""

    def __init__(self, dag_id: str, dag_run_id: str, detail: str = "") -> None:
        message = f"Airflow DAG run failed: dag_id={dag_id}, dag_run_id={dag_run_id}"
        if detail:
            message += f", detail={detail}"
        super().__init__(message)
        self.dag_id = dag_id
        self.dag_run_id = dag_run_id


class AirflowTimeoutError(DataSpokeError):
    """Raised when polling for an Airflow DAG run result times out."""

    def __init__(self, dag_id: str, dag_run_id: str, timeout_seconds: float) -> None:
        super().__init__(
            f"Airflow DAG run timed out after {timeout_seconds}s: "
            f"dag_id={dag_id}, dag_run_id={dag_run_id}"
        )
        self.dag_id = dag_id
        self.dag_run_id = dag_run_id
