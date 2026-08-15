"""``metric_conf`` invariants shared by the schema layer and the metrics service.

The measurement-window rule has two enforcement points that must never drift
apart: the Pydantic request models validate ``POST``/``PUT`` bodies
(:mod:`src.api.schemas.metrics`), and the service validates the *merged*
``metric_conf`` of a ``PATCH`` (:mod:`src.backend.metrics.service`). Both are
write-boundary checks of the same invariant, so the bound, the type rule and the
message live here — the one place both layers may import from.

Measurers hold no copy: they trust ``metric_conf`` by contract.

Spec: spec/feature/BACKEND.md §Metrics Service — Measurement window, Window bounds.
"""

from typing import Any

__all__ = [
    "MAX_TIME_WINDOW_SEC",
    "is_valid_time_window_sec",
    "time_window_sec_error",
]

#: Upper bound on ``metric_conf.time_window_sec`` — ten years in seconds.
#:
#: A *product* bound, not an arithmetic one: the window is a declared freshness
#: or validation SLO, and an SLO measured in decades asserts nothing. It also
#: sits five orders of magnitude inside what ``timedelta`` can represent, so the
#: measurers' ``now - timedelta(seconds=window)`` cannot overflow on a value the
#: write boundary admitted.
MAX_TIME_WINDOW_SEC = 315_360_000


def is_valid_time_window_sec(value: Any) -> bool:
    """Return whether *value* is an admissible ``time_window_sec``.

    ``bool`` is rejected explicitly: it subclasses ``int``, so without the guard
    ``{"time_window_sec": true}`` would slip through as a one-second window.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    return 1 <= value <= MAX_TIME_WINDOW_SEC


def time_window_sec_error(metric_type: str) -> str:
    """Return the complete rejection message for a bad window on *metric_type*.

    A function rather than a constant so no call site has to append the type
    itself and risk emitting a truncated sentence.
    """
    return (
        f"metric_conf.time_window_sec must be an int in [1, {MAX_TIME_WINDOW_SEC}] "
        f"for metric_type '{metric_type}'"
    )
