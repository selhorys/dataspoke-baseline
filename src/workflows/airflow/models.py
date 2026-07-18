"""Pydantic models for Airflow Stable REST API responses."""

import json
from enum import StrEnum
from typing import Any

from pydantic import BaseModel


class DagRunState(StrEnum):
    queued = "queued"
    running = "running"
    success = "success"
    failed = "failed"


class DagRunResponse(BaseModel):
    dag_run_id: str
    dag_id: str
    state: DagRunState
    conf: dict[str, Any] = {}
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


class XcomEntry(BaseModel):
    """Envelope returned by Airflow GET /xcomEntries/{key}.

    Airflow serializes the pushed value as a JSON string in some versions and as
    a parsed object in others.  ``parsed_value`` normalises both forms.
    """

    key: str
    value: Any = None
    timestamp: str | None = None

    @property
    def parsed_value(self) -> Any:
        """Return the deserialized XCom value.

        When Airflow returns ``value`` as a raw JSON string (older 3.x behaviour),
        JSON-decode it.  When it is already a dict/list/scalar, return as-is.
        """
        if isinstance(self.value, str):
            try:
                return json.loads(self.value)
            except (json.JSONDecodeError, ValueError):
                return self.value
        return self.value
