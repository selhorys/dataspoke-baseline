"""Measurer registry — maps metric-type names to async measurer functions."""

from collections.abc import Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from typing import Protocol

from src.shared.datahub.client import DataHubClient


class MeasurerFn(Protocol):
    """Protocol satisfied by every registered measurer coroutine."""

    async def __call__(
        self,
        datasets: list[str],
        metric_conf: dict[str, Any],
        *,
        datahub: DataHubClient,
        db: AsyncSession,
    ) -> tuple[dict[str, float], dict[str, Any]]: ...


_MEASURERS: dict[str, MeasurerFn] = {}


def register_measurer(name: str) -> Callable:
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
