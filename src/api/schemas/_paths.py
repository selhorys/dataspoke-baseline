"""Shared annotated path-parameter types for router boundary validation.

These types are applied to FastAPI path parameters to reject obviously
malformed input at the router layer, before any service or DB call.
"""

from typing import Annotated

from fastapi import Path

# UUID-shaped path parameter (lowercase hex format, 8-4-4-4-12).
UuidPath = Annotated[
    str,
    Path(
        pattern=(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}"
            r"-[0-9a-f]{4}-[0-9a-f]{12}$"
        )
    ),
]

# DataHub dataset URN path parameter.
DatasetUrnPath = Annotated[
    str,
    Path(
        min_length=1,
        max_length=512,
        pattern=r"^urn:li:dataset:\(.+\)$",
    ),
]
