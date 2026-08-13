"""Dataset URN parsing helpers.

DataHub dataset URNs follow the format
``urn:li:dataset:(urn:li:dataPlatform:<platform>,<name>,<origin>)``, where the
platform itself is a nested URN. This module owns the dataset-URN shape and the
dependency-free helpers that read its segments; every consumer imports from here
rather than re-deriving the format.
"""

import re

#: The dataset-URN shape every write boundary checks. ``\\Z`` rather than ``$``:
#: Python's ``$`` also matches just before a trailing newline, which would admit
#: a value the pydantic (rust-regex) write boundary rejects.
DATASET_URN_RE: re.Pattern[str] = re.compile(r"^urn:li:dataset:\(.+\)\Z")

PLATFORM_URN_PREFIX = "urn:li:dataPlatform:"
DATASET_URN_PLATFORM_PREFIX = f"urn:li:dataset:({PLATFORM_URN_PREFIX}"


def platform_from_dataset_urn(urn: str) -> str | None:
    """Return the platform id from a dataset URN, or None when it does not match.

    The platform id is the value between ``urn:li:dataPlatform:`` and the first
    comma of the URN inner tuple (e.g. ``postgres`` for a postgres dataset URN).
    Splitting on the **first** comma is what makes this exact: a platform id
    carries no comma, while the *name* segment may.
    """
    start = urn.find(DATASET_URN_PLATFORM_PREFIX)
    if start < 0:
        return None
    start += len(DATASET_URN_PLATFORM_PREFIX)
    end = urn.find(",", start)
    if end < 0:
        return None
    return urn[start:end] or None


def platform_urn_from_dataset_urn(urn: str) -> str | None:
    """Return the platform **URN** (``urn:li:dataPlatform:…``) from a dataset URN.

    The ``dataset_registry.platform_urn`` column mirrors the URN's first segment,
    which is the platform id re-prefixed. Built on
    :func:`platform_from_dataset_urn` so one parse serves both readings.
    """
    platform = platform_from_dataset_urn(urn)
    return f"{PLATFORM_URN_PREFIX}{platform}" if platform else None
