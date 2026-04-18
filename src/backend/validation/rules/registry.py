"""Rule evaluator registry — maps rule type names to async evaluator functions."""

from collections.abc import Awaitable, Callable
from typing import Any

_EVALUATORS: dict[str, Callable[..., Awaitable[Any]]] = {}


def register_rule(name: str) -> Callable:
    """Decorator that registers an async evaluator function under the given rule type name."""

    def decorator(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        _EVALUATORS[name] = fn
        return fn

    return decorator


def get_evaluator(name: str) -> Callable[..., Awaitable[Any]] | None:
    """Return the evaluator registered under *name*, or None if not found."""
    return _EVALUATORS.get(name)
