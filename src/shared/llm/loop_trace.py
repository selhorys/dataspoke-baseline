"""Return types for the ReAct-style tool-calling loop in LLMClient."""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class LoopTrace:
    iterations: int
    errors_per_iter: list[list[dict[str, str]]]
    final_errors: list[dict[str, str]]


@dataclass(frozen=True)
class LoopResult:
    payload: dict[str, Any]
    trace: LoopTrace
