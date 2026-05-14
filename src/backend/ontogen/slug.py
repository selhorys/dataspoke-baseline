"""Slug normalisation utilities for Ontogen node/edge IDs.

Spec: spec/feature/BACKEND.md §Ontology Generation Service
"""

import re
import unicodedata

SLUG_ID_RE = re.compile(r"^[a-z0-9_]{1,64}$")


def to_snake(text: str) -> str:
    """Convert *text* to a lowercase ASCII snake_case slug.

    Steps:
    1. NFKD normalise + ASCII encode (drops accents and non-ASCII).
    2. Lowercase.
    3. Collapse runs of non-alphanumeric chars to a single underscore.
    4. Strip leading/trailing underscores.
    5. Return ``"x"`` if the result is empty.
    """
    normalized = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "_", normalized.lower())
    slug = slug.strip("_")
    return slug or "x"


def make_snake_id(name: str, existing_ids: set[str]) -> str:
    """Derive a unique snake_case slug from *name*, avoiding *existing_ids*.

    Guarantees:
    - Result matches ``SLUG_ID_RE`` (a-z0-9_, 1–64 chars).
    - Result never contains ``__``.
    - Collisions are resolved by appending ``_1``, ``_2``, … until unique.
    """
    base = to_snake(name)
    base = base.replace("__", "_")
    candidate = base
    counter = 1
    while candidate in existing_ids or "__" in candidate:
        candidate = f"{base}_{counter}"
        counter += 1
    return candidate


def assert_node_id(node_id: str) -> None:
    """Raise ``ValueError`` if *node_id* is not a valid snake_case slug."""
    if not SLUG_ID_RE.match(node_id):
        raise ValueError(
            f"node_id {node_id!r} is not a valid slug (allowed: a-z 0-9 _, max 64 chars)"
        )


def assert_edge_id(edge_id: str) -> None:
    """Raise ``ValueError`` if *edge_id* is not a valid snake_case slug."""
    if not SLUG_ID_RE.match(edge_id):
        raise ValueError(
            f"edge_id {edge_id!r} is not a valid slug (allowed: a-z 0-9 _, max 64 chars)"
        )
