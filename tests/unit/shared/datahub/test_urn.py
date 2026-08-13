"""Unit tests for src/shared/datahub/urn.py — dataset URN platform extraction.

Covers platform_from_dataset_urn, which parses the platform id out of a DataHub
dataset URN. The helper is dependency-free (no SDK, no DB, no network).

Spec: spec/feature/BACKEND.md §Sync + mapping sweep — matcher derived from the
recipe's platform+database+schema_pattern/table_pattern; the sync sweep extracts
the platform from each dataset URN to enforce platform scoping before calling the
matcher.

Absorbs coverage of the two _parse_platform tests previously in
tests/unit/backend/dataset/test_service.py (removed when _parse_platform was
extracted to this shared helper).
"""

from src.shared.datahub.urn import platform_from_dataset_urn, platform_urn_from_dataset_urn

# ── postgres URN ─────────────────────────────────────────────────────────────


def test_postgres_urn_returns_postgres() -> None:
    """A canonical postgres dataset URN yields platform id 'postgres'.

    Spec: BACKEND.md §Sync + mapping sweep — platform extracted from URN to gate
    the matcher; postgres URN format is
    urn:li:dataset:(urn:li:dataPlatform:postgres,<name>,<env>).
    """
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.orders,DEV)"
    assert platform_from_dataset_urn(urn) == "postgres"


# ── kafka URN ─────────────────────────────────────────────────────────────────


def test_kafka_urn_returns_kafka() -> None:
    """A canonical kafka dataset URN yields platform id 'kafka'.

    Spec: BACKEND.md §Sync + mapping sweep — platform extracted from URN; kafka
    names appear as <platform_instance>.<topic> or bare <topic>.
    """
    urn = "urn:li:dataset:(urn:li:dataPlatform:kafka,example_kafka.imazon.orders.events,DEV)"
    assert platform_from_dataset_urn(urn) == "kafka"


# ── malformed / non-dataset URNs → None ──────────────────────────────────────


def test_malformed_string_returns_none() -> None:
    """An arbitrary non-URN string returns None (no platform extractable).

    Spec: BACKEND.md §Sync + mapping sweep — malformed recipes / unknown platforms
    map nothing; returning None lets the caller pick a fallback (e.g. 'unknown').
    """
    assert platform_from_dataset_urn("not-a-urn") is None


def test_non_dataset_urn_returns_none() -> None:
    """A valid URN that is not a dataset URN (e.g. corpuser) returns None.

    Spec: BACKEND.md §Sync + mapping sweep — the helper is specific to dataset URNs;
    entity URNs of other types do not carry the urn:li:dataPlatform: inner tuple.
    """
    assert platform_from_dataset_urn("urn:li:corpuser:foo") is None


def test_empty_string_returns_none() -> None:
    """An empty string returns None (no URN structure present).

    Spec: BACKEND.md §Sync + mapping sweep — malformed input maps nothing.
    """
    assert platform_from_dataset_urn("") is None


def test_comma_free_urn_returns_none() -> None:
    """A URN carrying the platform prefix but no terminating comma returns None.

    Spec: BACKEND.md §Sync + mapping sweep — the platform id is delimited by the
    first comma of the inner tuple; without one there is no extractable id.
    """
    assert platform_from_dataset_urn("urn:li:dataset:(urn:li:dataPlatform:postgres") is None


def test_repeated_prefix_without_comma_returns_none() -> None:
    """Repeated platform prefixes with no comma anywhere return None.

    Spec: BACKEND.md §Sync + mapping sweep — malformed input maps nothing. This
    input shape is scanned linearly: the prefix is located once and the comma
    searched forward from there, with no per-position retry.
    """
    assert platform_from_dataset_urn("urn:li:dataset:(urn:li:dataPlatform:" * 2000) is None


def test_empty_platform_segment_returns_none() -> None:
    """A URN whose platform segment is empty returns None, not an empty string.

    Spec: BACKEND.md §Sync + mapping sweep — callers substitute a fallback (e.g.
    'unknown') on None, so an absent platform must not surface as ''.
    """
    urn = "urn:li:dataset:(urn:li:dataPlatform:,example_db.catalog.orders,DEV)"
    assert platform_from_dataset_urn(urn) is None


# ── platform_urn_from_dataset_urn — the dataset_registry mirror column ────────


def test_platform_urn_reprefixes_the_platform_id() -> None:
    """The helper answers the URN's first segment, the `platform_urn` a filter reads.

    Spec: spec/DATAHUB_INTEGRATION.md §Dataset attribute sync — "`origin`,
    `platform_urn` | parsed from the dataset URN | `urn:li:dataset:(<platform_urn>,
    <name>,<origin>)` encodes both by definition".
    Spec: spec/API.md §`dataset_filter` grammar — "`platform_urn` | scalar | The URN's
    first segment — `urn:li:dataPlatform:…`".
    """
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.orders,DEV)"
    assert platform_urn_from_dataset_urn(urn) == "urn:li:dataPlatform:postgres"


def test_platform_urn_of_a_name_carrying_commas_reads_the_first_segment_only() -> None:
    """The platform id is delimited by the FIRST comma; the name may carry more.

    Spec: spec/API.md §`dataset_filter` grammar — `platform_urn` is the URN's first
    segment.
    """
    urn = "urn:li:dataset:(urn:li:dataPlatform:kafka,a,b.c,DEV)"
    assert platform_urn_from_dataset_urn(urn) == "urn:li:dataPlatform:kafka"


def test_platform_urn_of_a_malformed_urn_is_none() -> None:
    """A URN with no extractable platform yields None, never a bare prefix.

    A `"urn:li:dataPlatform:"` with nothing after it would be a registry value no
    dataset can carry and every `platform_urn` filter would silently mismatch.

    Spec: spec/feature/BACKEND_SCHEMA.md §dataset_registry — `platform_urn` is
    `TEXT` NULL, so "unparseable" has a representation of its own.
    """
    for urn in ["not-a-urn", "", "urn:li:corpuser:foo"]:
        assert platform_urn_from_dataset_urn(urn) is None


def test_platform_urn_of_an_empty_platform_segment_is_none() -> None:
    """Spec: spec/feature/BACKEND_SCHEMA.md §dataset_registry — `platform_urn` NULL
    rather than an empty-prefix value."""
    urn = "urn:li:dataset:(urn:li:dataPlatform:,example_db.catalog.orders,DEV)"
    assert platform_urn_from_dataset_urn(urn) is None
