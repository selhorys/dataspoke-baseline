"""Metrics measurer package — registry-dispatched async measurers.

Importing this package triggers the ``@register_measurer`` side-effects in each
measurer module so that ``get_measurer`` can resolve any registered name.

Built-in metric types:
- ``ingestion-freshness``: counts datasets ingested within the time window.
- ``validation-score``: sums per-dataset validation scores within the time window.
- ``doc-health``: counts datasets with complete table and column documentation.
"""

# Force-import measurer modules so their @register_measurer decorators run.
from src.backend.metrics.measurers import (  # noqa: F401
    doc_health,
    ingestion_freshness,
    validation_score,
)
from src.backend.metrics.measurers.registry import (
    DatasetVerdict,
    get_measurer,
    list_measurers,
)

__all__ = ["DatasetVerdict", "get_measurer", "list_measurers"]
