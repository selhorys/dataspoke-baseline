"""Shared admin request schemas.

Used by both the admin router and the internal activities router to ensure
consistent validation (URN pattern + list-length cap) on DataHub sync requests.
"""

from typing import Annotated

from pydantic import BaseModel, Field

# A single DataHub dataset URN with format and length constraints.
DatasetUrn = Annotated[
    str,
    Field(
        min_length=1,
        max_length=512,
        pattern=r"^urn:li:dataset:\(.+\)$",
    ),
]


class DatahubSyncRequest(BaseModel):
    """Request body for DataHub sync operations.

    ``dataset_urns=None`` (or omitted) triggers a full-sweep reconciliation.
    When provided, only the listed URNs are reconciled.  Capped at 10 000
    entries to prevent runaway requests.
    """

    dataset_urns: Annotated[list[DatasetUrn], Field(max_length=10_000)] | None = None
