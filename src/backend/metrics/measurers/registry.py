"""Measurer registry — maps metric-type names to async measurer functions."""

from collections.abc import Awaitable, Callable
from typing import Any

_MEASURERS: dict[str, Callable[..., Awaitable[tuple[float, dict[str, Any]]]]] = {}


def register_measurer(name: str) -> Callable:
    """Decorator: register an async measurer function under *name*."""

    def decorator(fn: Callable[..., Awaitable[tuple[float, dict[str, Any]]]]) -> Callable:
        _MEASURERS[name] = fn
        return fn

    return decorator


def get_measurer(name: str) -> Callable[..., Awaitable[tuple[float, dict[str, Any]]]] | None:
    """Return the measurer registered under *name*, or ``None``."""
    return _MEASURERS.get(name)


def list_measurers() -> list[str]:
    """Return sorted list of registered measurer names."""
    return sorted(_MEASURERS)
