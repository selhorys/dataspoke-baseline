"""Unit tests for the CLI-wrapper parent-URN parser (`_parse_cli_parent_urn`).

`_parse_cli_parent_urn` is a pure function (no infra) that extracts the parent
registered-source URN embedded in a `[CLI]` wrapper source's display name. The
name grammar is fixed by spec:

  spec/DATAHUB_INTEGRATION.md §Ingestion Source Sync — 'Ad-hoc CLI wrapper for a
  registered source': when `datahub ingest` runs a recipe carrying a
  `pipeline_name`, DataHub auto-creates an ad-hoc CLI wrapper source whose
  `dataHubIngestionSourceInfo.name` is `[CLI] <type> [<pipeline_name>]` — the
  configured `pipeline_name` embedded verbatim in the trailing brackets. When
  DataSpoke runs a registered source, that `pipeline_name` is the parent
  registered-source URN, so the name reads `[CLI] <type> [<parent_source_urn>]`.

  spec/feature/BACKEND.md §Sync sweep step 3 (Observed enrichment) — the wrapper
  inherits the pipeline_name link by parsing that parent URN out of its display
  name; the parser returns None (→ fallback to matched/medium) when the name has
  no parseable parent registered-source URN.

Assertions derive from that spec name grammar, not from the impl's regex/rfind
internals.

Spec: spec/DATAHUB_INTEGRATION.md §Ingestion Source Sync
Spec: spec/feature/BACKEND.md §Sync sweep step 3 (Observed enrichment)
"""

from __future__ import annotations

import pytest

from src.backend.ingestion.service import _parse_cli_parent_urn

# A canonical parent registered-source URN (a real `dataHubIngestionSource` URN
# carrying a guid id, the form DataHub stamps as pipeline_name for a registered run).
_PARENT_URN = "urn:li:dataHubIngestionSource:f48f8a53-0e4b-4a1f-9c33-aa00bb11cc22"


# ── Canonical wrapper name → parent URN ───────────────────────────────────────


class TestCanonicalWrapperNameParses:
    """Spec: `[CLI] <type> [<parent_source_urn>]` yields the bracketed parent URN."""

    def test_canonical_cli_wrapper_name_returns_parent_urn(self) -> None:
        """`[CLI] postgres [urn:li:dataHubIngestionSource:…]` → that URN.

        Spec: DATAHUB_INTEGRATION.md §Ingestion Source Sync — the wrapper name is
        `[CLI] <type> [<pipeline_name>]` with the parent registered-source URN
        embedded verbatim in the trailing brackets.
        """
        name = f"[CLI] postgres [{_PARENT_URN}]"
        assert _parse_cli_parent_urn(name) == _PARENT_URN

    def test_other_source_type_still_parses(self) -> None:
        """The `<type>` token is arbitrary — a kafka wrapper parses the same way.

        Spec: DATAHUB_INTEGRATION.md §Ingestion Source Sync — only the trailing
        bracketed value (the pipeline_name) is the parent URN; the type token is
        not constrained.
        """
        name = f"[CLI] kafka [{_PARENT_URN}]"
        assert _parse_cli_parent_urn(name) == _PARENT_URN


# ── System CLI wrappers with no parent URN → None ─────────────────────────────


class TestSystemWrappersHaveNoParent:
    """Spec: a `[CLI]` name without a trailing parent-URN bracket has no parent to
    inherit from → None (fallback to matched/medium)."""

    @pytest.mark.parametrize(
        "name",
        [
            "[CLI] datahub-documents",
            "[CLI] datahub-gc",
            "[CLI] postgres",
        ],
    )
    def test_cli_name_without_trailing_bracket_is_none(self, name: str) -> None:
        """A `[CLI] <type>` name with no `[<urn>]` suffix → None.

        Spec: feature/BACKEND.md §Sync sweep step 3 — if the parent URN cannot be
        parsed from the wrapper's display name, the ad-hoc source inherits nothing.
        DataHub's system CLI wrappers (datahub-gc, datahub-documents) carry no
        pipeline_name, so they have no trailing parent bracket.
        """
        assert _parse_cli_parent_urn(name) is None


# ── Non-`[CLI]` names → None ──────────────────────────────────────────────────


class TestNonCliNamesAreNone:
    """Spec: only `[CLI] ` wrapper names embed a parent URN; any other name → None."""

    @pytest.mark.parametrize(
        "name",
        [
            "uc1-datahub-managed-secretref-postgres",
            "My Postgres",
            # A registered source's own name happens to end in a bracketed URN but
            # is not a [CLI] wrapper → not parseable as a CLI parent.
            f"Analytics [{_PARENT_URN}]",
        ],
    )
    def test_non_cli_name_is_none(self, name: str) -> None:
        """A name that does not start with `[CLI] ` → None.

        Spec: DATAHUB_INTEGRATION.md §Ingestion Source Sync — the parent-URN
        embedding is specific to the `[CLI] <type> [<pipeline_name>]` wrapper-name
        grammar; registered/UI source names are not parsed for a parent.
        """
        assert _parse_cli_parent_urn(name) is None


# ── Malformed `[CLI]` names → None ────────────────────────────────────────────


class TestMalformedNamesAreNone:
    """Spec: a `[CLI]` name whose trailing bracket does not hold a
    `dataHubIngestionSource` URN yields no parent → None (fallback)."""

    def test_missing_trailing_bracket_is_none(self) -> None:
        """No closing `]` → None.

        Spec: feature/BACKEND.md §Sync sweep step 3 — unparseable wrapper name →
        inherit nothing.
        """
        assert _parse_cli_parent_urn(f"[CLI] postgres [{_PARENT_URN}") is None

    def test_empty_brackets_is_none(self) -> None:
        """Empty trailing brackets `[]` hold no URN → None.

        Spec: DATAHUB_INTEGRATION.md §Ingestion Source Sync — the bracketed value
        must be the parent `dataHubIngestionSource` URN.
        """
        assert _parse_cli_parent_urn("[CLI] postgres []") is None

    def test_bracketed_non_ingestion_source_urn_is_none(self) -> None:
        """Bracketed content that is not a `dataHubIngestionSource` URN → None.

        A dataset URN in the brackets is not a parent ingestion-source URN.
        Spec: DATAHUB_INTEGRATION.md §Ingestion Source Sync — the parent is a
        `urn:li:dataHubIngestionSource:…` URN specifically.
        """
        dataset_urn = (
            "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"
        )
        assert _parse_cli_parent_urn(f"[CLI] postgres [{dataset_urn}]") is None

    def test_trailing_extra_brackets_is_none(self) -> None:
        """A trailing extra bracket pair after the URN → None.

        The parser anchors on the FINAL bracket per the DataHub grammar (the
        pipeline_name is the last bracketed token); a `[CLI] <type> [<urn>] [extra]`
        name's final bracket holds `extra`, which is not a dataHubIngestionSource
        URN, so the parse fails.
        Spec: DATAHUB_INTEGRATION.md §Ingestion Source Sync — pipeline_name embedded
        in the trailing brackets.
        """
        name = f"[CLI] postgres [{_PARENT_URN}] [extra]"
        assert _parse_cli_parent_urn(name) is None


# ── Absent input → None ───────────────────────────────────────────────────────


class TestAbsentInput:
    """Spec: absent name has no parent URN → None."""

    @pytest.mark.parametrize("name", [None, ""])
    def test_none_or_empty_is_none(self, name: str | None) -> None:
        """None / empty-string input → None (no name to parse).

        Spec: feature/BACKEND.md §Sync sweep step 3 — a wrapper with no parseable
        parent inherits nothing.
        """
        assert _parse_cli_parent_urn(name) is None
