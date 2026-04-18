"""Metrics measurer package — registry-dispatched async measurers.

Importing this package triggers the ``@register_measurer`` side-effects in each
measurer module so that ``get_measurer`` can resolve any registered name.
"""

# Force-import measurer modules so their @register_measurer decorators run.
from src.backend.metrics.measurers import poorly_documented, stale_datasets  # noqa: F401
from src.backend.metrics.measurers.registry import get_measurer, list_measurers

__all__ = ["get_measurer", "list_measurers"]
