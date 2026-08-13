"""Measurer registry — maps metric-type names to async measurer functions."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.datahub.client import DataHubClient


@dataclass(frozen=True)
class DatasetVerdict:
    """One dataset's outcome for one metric run.

    Verdicts cover **every** dataset in scope, not only the failing ones: full
    coverage is what makes "in scope but never evaluated" (``unknown`` on
    ``GET /spoke/governance/metric/{metric_id}/dataset``) distinguishable from
    "evaluated and passing", which a failures-only return cannot express. The
    service derives the failures-only ``metric_results.breakdown`` from these,
    so the stored breakdown and the per-dataset store can never disagree.

    ``evidence_at`` is the per-dataset evidence timestamp — the resolved
    ingestion evidence time for ``ingestion-freshness``, the counted result's
    ``data_time`` for ``validation-score``, and ``None`` for ``doc-health``,
    whose documentation state carries no timestamp.

    Spec: spec/feature/BACKEND.md §Metrics Service — Verdict contract.
    """

    urn: str
    met: bool
    evidence_at: datetime | None = None
    detail: dict[str, Any] = field(default_factory=dict)


class MeasurerFn(Protocol):
    """Protocol satisfied by every registered measurer coroutine."""

    async def __call__(
        self,
        datasets: list[str],
        metric_conf: dict[str, Any],
        *,
        datahub: DataHubClient,
        db: AsyncSession,
    ) -> tuple[dict[str, float], list[DatasetVerdict]]: ...


_MEASURERS: dict[str, MeasurerFn] = {}


def register_measurer(name: str) -> Callable[[MeasurerFn], MeasurerFn]:
    """Decorator: register an async measurer function under *name*."""

    def decorator(fn: MeasurerFn) -> MeasurerFn:
        _MEASURERS[name] = fn
        return fn

    return decorator


def get_measurer(name: str) -> MeasurerFn | None:
    """Return the measurer registered under *name*, or ``None``."""
    return _MEASURERS.get(name)


def list_measurers() -> list[str]:
    """Return sorted list of registered measurer names."""
    return sorted(_MEASURERS)
