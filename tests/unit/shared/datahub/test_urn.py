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

from src.shared.datahub.urn import platform_from_dataset_urn

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
