"""Unit tests for the CLI/ad-hoc source classifier (`_is_ad_hoc`).

`_is_ad_hoc` is a pure function (no infra) that classifies a DATAHUB_MANAGED
ingestion source as CLI/ad-hoc. The classification rule is fixed by spec:

  spec/DATAHUB_INTEGRATION.md §Ingestion Source Sync — 'Ad-hoc (CLI) source
  classification':
    - a source created by `datahub ingest` or a UI/API Run click is a CLI source —
      still listed by listIngestionSources, so it syncs as DATAHUB_MANAGED, but
      DataSpoke flags it ad_hoc=true.
    - The decisive marker is config.executorId starting `__datahub_cli_` (primary);
      fallbacks are a `cli-`-prefixed source URN id
      (urn:li:dataHubIngestionSource:cli-<guid>) and a `[CLI] ` name prefix.
    - pipeline_name is NOT a marker (optional, and present on UI sources too).

Assertions derive from that spec rule, not from the impl's line-by-line ordering.

Spec: spec/DATAHUB_INTEGRATION.md §Ingestion Source Sync
"""

from __future__ import annotations

import pytest

from src.backend.ingestion.service import _is_ad_hoc

# A regular UI-created DataHub source: non-CLI executor, opaque guid URN id,
# plain display name. None of the three markers is present.
_REGULAR_EXECUTOR = "default"
_REGULAR_URN = "urn:li:dataHubIngestionSource:f3a9c1d2-0e4b-4a1f-9c33-aa00bb11cc22"
_REGULAR_NAME = "My Postgres"


# ── Marker presence: each marker alone flags ad-hoc ───────────────────────────


class TestEachMarkerFlagsAdHoc:
    """Spec: each of the three markers, present in isolation, flags the source
    ad_hoc=true (executorId primary; URN-id and name-prefix fallbacks)."""

    def test_executor_id_cli_prefix_is_ad_hoc(self) -> None:
        """config.executorId starting `__datahub_cli_` → ad_hoc=true (primary marker).

        Spec: DATAHUB_INTEGRATION.md §Ingestion Source Sync — 'decisive marker is
        config.executorId starting __datahub_cli_'.
        """
        assert (
            _is_ad_hoc("__datahub_cli_ingestion", _REGULAR_URN, _REGULAR_NAME) is True
        )

    def test_cli_prefixed_urn_id_is_ad_hoc(self) -> None:
        """A `cli-`-prefixed source URN id → ad_hoc=true (URN fallback).

        The id is the last colon-segment of urn:li:dataHubIngestionSource:<id>.
        Spec: DATAHUB_INTEGRATION.md §Ingestion Source Sync — 'a cli-prefixed source
        URN id (urn:li:dataHubIngestionSource:cli-<guid>)'.
        """
        cli_urn = "urn:li:dataHubIngestionSource:cli-abc123"
        assert _is_ad_hoc(_REGULAR_EXECUTOR, cli_urn, _REGULAR_NAME) is True

    def test_cli_name_prefix_is_ad_hoc(self) -> None:
        """A display name starting `[CLI] ` (with trailing space) → ad_hoc=true.

        Spec: DATAHUB_INTEGRATION.md §Ingestion Source Sync — 'a [CLI] name prefix'.
        """
        assert _is_ad_hoc(_REGULAR_EXECUTOR, _REGULAR_URN, "[CLI] postgres") is True


# ── Regular (UI) source: no marker → not ad-hoc ───────────────────────────────


class TestRegularSourceNotAdHoc:
    """Spec: a regular UI/API-managed source carries none of the three markers and
    must classify ad_hoc=false."""

    def test_regular_ui_source_is_not_ad_hoc(self) -> None:
        """A UI source (executorId 'default', guid URN id, plain name) → ad_hoc=false.

        Spec: DATAHUB_INTEGRATION.md §Ingestion Source Sync — UI sources are
        DATAHUB_MANAGED but not ad-hoc.
        """
        assert (
            _is_ad_hoc(_REGULAR_EXECUTOR, _REGULAR_URN, _REGULAR_NAME) is False
        )

    def test_all_none_is_not_ad_hoc(self) -> None:
        """All inputs None (no signal at all) → ad_hoc=false (default).

        Spec: DATAHUB_INTEGRATION.md §Ingestion Source Sync — absence of every
        marker means the source is not ad-hoc.
        """
        assert _is_ad_hoc(None, None, None) is False


# ── Precedence / combinations ─────────────────────────────────────────────────


class TestMarkerCombinations:
    """Spec: ad_hoc is true when ANY marker holds — the markers compose with OR.
    These cases confirm that any marker (or several) independently yields true, and
    that markers that don't quite match the required pattern don't fire.
    """

    @pytest.mark.parametrize(
        ("executor_id", "source_urn", "name"),
        [
            # executor_id marker fires even when URN/name are regular.
            ("__datahub_cli_ingestion", _REGULAR_URN, _REGULAR_NAME),
            # URN-id marker fires even when executor/name are regular.
            (_REGULAR_EXECUTOR, "urn:li:dataHubIngestionSource:cli-xyz", _REGULAR_NAME),
            # name marker fires even when executor/URN are regular.
            (_REGULAR_EXECUTOR, _REGULAR_URN, "[CLI] kafka"),
            # all three markers present.
            ("__datahub_cli_ingestion", "urn:li:dataHubIngestionSource:cli-q", "[CLI] x"),
            # executor + name markers; URN regular.
            ("__datahub_cli_ingestion", _REGULAR_URN, "[CLI] x"),
        ],
    )
    def test_any_marker_yields_ad_hoc(
        self, executor_id: str, source_urn: str, name: str
    ) -> None:
        """Any single marker (or combination) → ad_hoc=true (OR semantics).

        Spec: DATAHUB_INTEGRATION.md §Ingestion Source Sync — the markers are
        alternatives; the source is ad-hoc when any holds.
        """
        assert _is_ad_hoc(executor_id, source_urn, name) is True

    @pytest.mark.parametrize(
        ("executor_id", "source_urn", "name"),
        [
            # executorId merely *containing* (not starting with) the prefix → no marker.
            ("ingest__datahub_cli_x", _REGULAR_URN, _REGULAR_NAME),
            # URN id 'cli' without the required hyphen → not a cli- prefix.
            (_REGULAR_EXECUTOR, "urn:li:dataHubIngestionSource:client-1", _REGULAR_NAME),
            # name '[CLI]' without the required trailing space → no marker.
            (_REGULAR_EXECUTOR, _REGULAR_URN, "[CLI]postgres"),
        ],
    )
    def test_near_miss_markers_do_not_fire(
        self, executor_id: str, source_urn: str, name: str
    ) -> None:
        """Near-miss values that don't match the exact marker pattern → ad_hoc=false.

        Spec: DATAHUB_INTEGRATION.md §Ingestion Source Sync — markers are
        prefix-anchored (`__datahub_cli_`, `cli-`, `[CLI] `).
        """
        assert _is_ad_hoc(executor_id, source_urn, name) is False
