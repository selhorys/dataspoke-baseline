"""Dataset URN parsing helpers.

DataHub dataset URNs follow the format
``urn:li:dataset:(urn:li:dataPlatform:<platform>,<name>,<origin>)``, where the
platform itself is a nested URN. This module exposes a dependency-free helper to
extract the platform id from such a URN.
"""

import re

DATASET_URN_PLATFORM_RE = re.compile(r"urn:li:dataset:\(urn:li:dataPlatform:([^,]+),")


def platform_from_dataset_urn(urn: str) -> str | None:
    """Return the platform id from a dataset URN, or None when it does not match.

    The platform id is the value between ``urn:li:dataPlatform:`` and the first
    comma of the URN inner tuple (e.g. ``postgres`` for a postgres dataset URN).
    """
    m = DATASET_URN_PLATFORM_RE.search(urn)
    return m.group(1) if m else None
