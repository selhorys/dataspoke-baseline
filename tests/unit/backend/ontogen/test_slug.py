"""Unit tests for src/backend/ontogen/slug.py — slug normalisation utilities.

Spec: spec/feature/BACKEND.md §Ontology Generation Service
      BACKEND_LLM.md §Ontogen Validator (SLUG_FORMAT); impl src/backend/ontogen/slug.py

Each test pins one contract of the slug normaliser / validator; no parametrize
unless the test is naturally table-driven (avoid over-abstracting single assertions).
"""

import pytest

from src.backend.ontogen.slug import (
    assert_edge_id,
    assert_node_id,
    make_snake_id,
    to_snake,
)

# ── to_snake ──────────────────────────────────────────────────────────────────


def test_to_snake_replaces_whitespace_with_underscore() -> None:
    """Spec: impl slug.py — to_snake collapses runs of non-alphanumeric chars to '_'.
    'Order Line' (single space) → 'order_line'.
    """
    assert to_snake("Order Line") == "order_line"


def test_to_snake_lowercases_and_strips_punctuation() -> None:
    """Spec: impl slug.py — to_snake lowercases and collapses non-alphanumeric to '_'.
    'Book-Title' (hyphen) → 'book_title'; hyphen is treated as a separator.
    """
    assert to_snake("Book-Title") == "book_title"


def test_to_snake_collapses_repeated_whitespace() -> None:
    """Spec: impl slug.py — to_snake collapses runs of non-alphanumeric (including multiple spaces)
    to a single '_', then strips leading/trailing '_'.
    '  Has   Edition  ' → 'has_edition'.
    """
    assert to_snake("  Has   Edition  ") == "has_edition"


def test_to_snake_empty_returns_x() -> None:
    """Spec: impl slug.py — to_snake returns 'x' when the result is empty after stripping.
    Empty string input has no alphanumeric chars → result is 'x'.
    """
    assert to_snake("") == "x"


def test_to_snake_only_punctuation_returns_x() -> None:
    """Spec: impl slug.py — to_snake returns 'x' when all chars are non-alphanumeric.
    '---' strips to '' → returns 'x'.
    """
    assert to_snake("---") == "x"


def test_to_snake_unicode_normalized_to_ascii() -> None:
    """Spec: impl slug.py — NFKD normalise + ASCII encode drops accents and non-ASCII.
    'Café' → NFKD → 'Café' → encode ASCII ignore → 'Cafe' → lower → 'cafe'.
    """
    assert to_snake("Café") == "cafe"


# ── make_snake_id ─────────────────────────────────────────────────────────────


def test_make_snake_id_disambiguates_on_collision() -> None:
    """Spec: impl slug.py — make_snake_id appends '_1', '_2', … on collision.
    make_snake_id('Order', {'order'}) must return 'order_1', not 'order'.
    """
    result = make_snake_id("Order", {"order"})
    assert result == "order_1", (
        f"Expected 'order_1' when 'order' already in existing_ids; got {result!r}. "
        "Spec: impl slug.py — collision suffix appended"
    )


def test_make_snake_id_never_produces_double_underscore() -> None:
    """Spec: impl slug.py — make_snake_id result never contains '__'.
    Input 'a__b' normalises via to_snake to 'a_b'; make_snake_id also collapses
    any residual __ so the final id is free of double-underscores.
    """
    result = make_snake_id("a__b", set())
    assert "__" not in result, (
        f"make_snake_id must never produce '__'; got {result!r}. "
        "Spec: impl slug.py — __ is reserved as the triple-id separator"
    )
    assert result == "a_b"


# ── assert_node_id / assert_edge_id ──────────────────────────────────────────


def test_assert_node_id_rejects_hyphen() -> None:
    """Spec: impl slug.py — SLUG_ID_RE = ^[a-z0-9_]{1,64}$ (no hyphens).
    assert_node_id('has-edition') must raise ValueError because hyphen is not allowed.
    """
    with pytest.raises(ValueError):
        assert_node_id("has-edition")


def test_assert_edge_id_rejects_uppercase() -> None:
    """Spec: impl slug.py — assert_edge_id raises ValueError for uppercase chars.
    'HasEdition' contains uppercase which is not in ^[a-z0-9_]{1,64}$.
    """
    with pytest.raises(ValueError):
        assert_edge_id("HasEdition")


def test_assert_node_id_accepts_valid_snake_case() -> None:
    """Spec: impl slug.py — assert_node_id accepts a valid snake_case slug without exception.
    'book_title' matches ^[a-z0-9_]{1,64}$ — no exception must be raised.
    """
    assert_node_id("book_title")  # must not raise
