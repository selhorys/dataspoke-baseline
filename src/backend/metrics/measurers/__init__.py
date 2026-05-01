"""Metrics measurer package — registry-dispatched async measurers.

Importing this package triggers the ``@register_measurer`` side-effects in each
measurer module so that ``get_measurer`` can resolve any registered name.

Baseline measurers (registered under spec ``aggregation`` keys):
- ``pct_fresh``: fresh/stale based on latest INGESTION.COMPLETE event recency.
- ``pct_rules_passing``: rules_passing/rules_failing based on latest validation results.
"""

# Force-import measurer modules so their @register_measurer decorators run.
from src.backend.metrics.measurers import ingestion_freshness, validation_score  # noqa: F401
from src.backend.metrics.measurers.registry import get_measurer, list_measurers

__all__ = ["get_measurer", "list_measurers"]
