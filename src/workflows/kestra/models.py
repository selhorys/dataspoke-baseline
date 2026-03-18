"""Pydantic models for Kestra REST API responses."""

from enum import Enum

from pydantic import BaseModel


class ExecutionStatus(str, Enum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    SUCCESS = "SUCCESS"
    WARNING = "WARNING"
    FAILED = "FAILED"
    KILLING = "KILLING"
    KILLED = "KILLED"
    RESTARTED = "RESTARTED"
    QUEUED = "QUEUED"
    RETRYING = "RETRYING"
    RETRIED = "RETRIED"
    CANCELLED = "CANCELLED"


class ExecutionResponse(BaseModel):
    id: str
    namespace: str
    flowId: str
    state: dict
    inputs: dict | None = None
    outputs: dict | None = None

    @property
    def status(self) -> ExecutionStatus:
        current = self.state.get("current", "CREATED")
        return ExecutionStatus(current)

    @property
    def is_terminal(self) -> bool:
        return self.status in (
            ExecutionStatus.SUCCESS,
            ExecutionStatus.WARNING,
            ExecutionStatus.FAILED,
            ExecutionStatus.KILLED,
        )


class FlowResponse(BaseModel):
    id: str
    namespace: str
    revision: int | None = None
