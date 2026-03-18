"""Kestra-specific error types."""

from src.shared.exceptions import DataSpokeError


class KestraExecutionFailedError(DataSpokeError):
    """Raised when a Kestra execution completes with FAILED status."""

    def __init__(self, flow_id: str, execution_id: str, detail: str = ""):
        message = f"Kestra execution failed: flow={flow_id}, execution={execution_id}"
        if detail:
            message += f", detail={detail}"
        super().__init__(message)
        self.flow_id = flow_id
        self.execution_id = execution_id


class KestraTimeoutError(DataSpokeError):
    """Raised when polling for a Kestra execution result times out."""

    def __init__(self, flow_id: str, execution_id: str, timeout_seconds: float):
        super().__init__(
            f"Kestra execution timed out after {timeout_seconds}s: "
            f"flow={flow_id}, execution={execution_id}"
        )
        self.flow_id = flow_id
        self.execution_id = execution_id


def parse_execution_error(execution_response: dict) -> str:
    """Extract a human-readable error message from a failed Kestra execution."""
    outputs = execution_response.get("outputs") or {}
    if isinstance(outputs, dict):
        for task_output in outputs.values():
            if isinstance(task_output, dict) and task_output.get("error"):
                return str(task_output["error"])
    task_runs = execution_response.get("taskRunList") or []
    for task_run in task_runs:
        state = task_run.get("state", {})
        if state.get("current") == "FAILED":
            return f"Task '{task_run.get('taskId', '?')}' failed"
    return "Unknown execution error"
