"""Pydantic models for Airflow Stable REST API responses."""

from enum import Enum

from pydantic import BaseModel


class DagRunState(str, Enum):
    queued = "queued"
    running = "running"
    success = "success"
    failed = "failed"


class DagRunResponse(BaseModel):
    dag_run_id: str
    dag_id: str
    state: DagRunState
    conf: dict = {}
    logical_date: str | None = None
    start_date: str | None = None
    end_date: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.state in (DagRunState.success, DagRunState.failed)

    @property
    def status(self) -> DagRunState:
        """Alias for state — backward compatibility with code that used .status."""
        return self.state
