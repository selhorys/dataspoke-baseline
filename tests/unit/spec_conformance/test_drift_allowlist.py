"""Tests for ``assert_drift_allowlist`` — the mechanism every conformance check rests on.

The conformance suites in this package all funnel their result through
``assert_drift_allowlist``. Today every one of those call sites compares an empty (or
exactly-matching) set against its allowlist, so neither failure branch of the helper is
taken by them. Without the tests below, deleting either branch — or the whole helper
body — leaves the rest of the package green, and the bidirectional guarantee that
justifies having allowlists at all would be unenforced.

Spec: spec/TESTING.md §Assertion Discipline — "A test that passes without proving
anything is worse than no test"; "Author assertions so that a passing result is only
reachable when the spec'd behavior actually occurred."

Unit-tier: pure function calls, no I/O.
"""

import pytest

from ._api_md import assert_drift_allowlist


class TestUndeclaredDriftFails:
    """Branch 1: drift that is not on the allowlist must fail (new drift is caught)."""

    def test_drift_absent_from_allowlist_raises(self) -> None:
        with pytest.raises(AssertionError) as excinfo:
            assert_drift_allowlist(
                {"BRAND_NEW_DRIFT"},
                frozenset(),
                what="example mismatch",
                allowlist_name="EXAMPLE_ALLOWLIST",
            )
        message = str(excinfo.value)
        assert "BRAND_NEW_DRIFT" in message, (
            f"The failure must name the offending entry so the author can act on it; got: {message}"
        )
        assert "EXAMPLE_ALLOWLIST" in message, (
            f"The failure must name the allowlist to add the entry to; got: {message}"
        )

    def test_partially_allowlisted_drift_raises_for_the_remainder(self) -> None:
        """An allowlist covering some drift must not excuse the rest of it."""
        with pytest.raises(AssertionError) as excinfo:
            assert_drift_allowlist(
                {"KNOWN_ONE", "UNKNOWN_TWO"},
                frozenset({"KNOWN_ONE"}),
                what="example mismatch",
                allowlist_name="EXAMPLE_ALLOWLIST",
            )
        message = str(excinfo.value)
        assert "UNKNOWN_TWO" in message
        assert "KNOWN_ONE" not in message, (
            f"An allowlisted entry must not be reported as undeclared drift; got: {message}"
        )


class TestStaleAllowlistFails:
    """Branch 2: an allowlist entry that no longer drifts must fail, forcing its deletion.

    This is the branch that stops the list rotting into a rubber stamp. A resolved entry
    left in place would silently absorb a later regression that reintroduces the same
    mismatch.
    """

    def test_resolved_allowlist_entry_raises(self) -> None:
        with pytest.raises(AssertionError) as excinfo:
            assert_drift_allowlist(
                set(),
                frozenset({"ALREADY_RESOLVED"}),
                what="example mismatch",
                allowlist_name="EXAMPLE_ALLOWLIST",
            )
        message = str(excinfo.value)
        assert "ALREADY_RESOLVED" in message, (
            f"The failure must name the stale entry so it can be deleted; got: {message}"
        )
        assert "EXAMPLE_ALLOWLIST" in message
        assert "delete" in message.lower(), (
            f"The failure must instruct the author to delete the entry, not to widen the "
            f"allowlist; got: {message}"
        )

    def test_resolved_entry_raises_even_alongside_live_drift(self) -> None:
        """A still-drifting sibling must not mask a resolved entry."""
        with pytest.raises(AssertionError) as excinfo:
            assert_drift_allowlist(
                {"STILL_DRIFTING"},
                frozenset({"STILL_DRIFTING", "ALREADY_RESOLVED"}),
                what="example mismatch",
                allowlist_name="EXAMPLE_ALLOWLIST",
            )
        assert "ALREADY_RESOLVED" in str(excinfo.value)


class TestMatchingSetsPass:
    """The passing cases, so the helper is not merely 'always raises'."""

    def test_exact_match_passes(self) -> None:
        assert_drift_allowlist(
            {"A", "B"},
            frozenset({"A", "B"}),
            what="example mismatch",
            allowlist_name="EXAMPLE_ALLOWLIST",
        )

    def test_no_drift_and_empty_allowlist_passes(self) -> None:
        """The state every clean conformance check is in — must not raise."""
        assert_drift_allowlist(
            set(),
            frozenset(),
            what="example mismatch",
            allowlist_name="EXAMPLE_ALLOWLIST",
        )

    def test_accepts_any_iterable_not_just_a_set(self) -> None:
        """Call sites pass generator/set comprehensions; duplicates must not matter."""
        assert_drift_allowlist(
            (code for code in ["A", "A", "B"]),
            frozenset({"A", "B"}),
            what="example mismatch",
            allowlist_name="EXAMPLE_ALLOWLIST",
        )
