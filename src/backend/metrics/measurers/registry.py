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

    Verdicts cover every dataset the measurer **evaluated**, not only the failing
    ones: covering the passing ones too is what makes "carries no verdict"
    (``unknown`` on ``GET /spoke/governance/metric/{metric_id}/dataset``)
    distinguishable from "evaluated and passing", which a failures-only return
    cannot express. The service derives the failures-only
    ``metric_results.breakdown`` from these, so the stored breakdown and the
    per-dataset store can never disagree.

    The evaluated set is the scanned scope for ``ingestion-freshness`` and
    ``doc-health``. ``validation-score`` is the exception: it evaluates only the
    datasets carrying a ``validation_configs`` row and deliberately returns **no**
    verdict for the rest, so an unconfigured dataset reads ``unknown`` rather than
    failing for a cadence it never declared. A verdict list shorter than the scope
    is therefore expected, not a measurer bug.

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
    """Protocol satisfied by every registered measurer coroutine.

    ``now`` is the run's **measurement instant**, read once by the service before
    it dispatches and passed to every measurer, so one run cannot date two
    measurers differently and a scheduled run can be anchored to the interval it
    is *for* rather than the one it executed in. It is required of every measurer
    whether or not the measurer's own logic reads it — the same uniformity
    ``datahub`` and ``db`` already carry.

    Spec: spec/feature/BACKEND.md §Metrics Service — Measurement instant.
    """

    async def __call__(
        self,
        datasets: list[str],
        metric_conf: dict[str, Any],
        *,
        datahub: DataHubClient,
        db: AsyncSession,
        now: datetime,
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
