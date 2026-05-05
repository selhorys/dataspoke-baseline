"""Unit tests — timeseries security: quote_table_ref, identifier regex, password resolution (Group A7).

Spec sources:
- spec/feature/BACKEND.md §SQL-Based Timeseries Engine
- src/backend/validation/timeseries.py _IDENTIFIER_RE (\\A/\\Z anchors, 63-char cap)
"""

import re
from unittest.mock import patch

import pytest

from src.backend.validation.timeseries import (
    _IDENTIFIER_RE,
    _resolve_postgresql_password,
    quote_table_ref,
)


# ── quote_table_ref — platform guard ─────────────────────────────────────────


def test_quote_table_ref_rejects_non_postgres_platform():
    """quote_table_ref raises NotImplementedError for non-postgres platforms."""
    with pytest.raises(NotImplementedError, match="bigquery"):
        quote_table_ref("bigquery", {"schema_name": "ds", "table": "t"})


def test_quote_table_ref_rejects_mysql_platform():
    """quote_table_ref raises NotImplementedError for mysql."""
    with pytest.raises(NotImplementedError):
        quote_table_ref("mysql", {"schema_name": "s", "table": "t"})


# ── quote_table_ref — missing parts ──────────────────────────────────────────


def test_quote_table_ref_raises_on_missing_schema():
    """quote_table_ref raises ValueError when schema_name is missing."""
    with pytest.raises(ValueError, match="schema_name"):
        quote_table_ref("postgres", {"table": "orders"})


def test_quote_table_ref_raises_on_missing_table():
    """quote_table_ref raises ValueError when table is missing."""
    with pytest.raises(ValueError, match="table"):
        quote_table_ref("postgres", {"schema_name": "public"})


def test_quote_table_ref_raises_on_empty_schema():
    """quote_table_ref raises ValueError when schema_name is empty string."""
    with pytest.raises(ValueError, match="schema_name"):
        quote_table_ref("postgres", {"schema_name": "", "table": "orders"})


def test_quote_table_ref_raises_on_empty_table():
    """quote_table_ref raises ValueError when table is empty string."""
    with pytest.raises(ValueError, match="table"):
        quote_table_ref("postgres", {"schema_name": "public", "table": ""})


# ── quote_table_ref — identifier validation ───────────────────────────────────


def test_quote_table_ref_rejects_sql_injection_in_schema():
    """Security: SQL injection in schema_name fails identifier regex → ValueError."""
    with pytest.raises(ValueError, match="schema_name"):
        quote_table_ref("postgres", {"schema_name": "foo; DROP TABLE orders", "table": "orders"})


def test_quote_table_ref_rejects_space_in_schema():
    """Security: space in schema_name fails identifier regex → ValueError."""
    with pytest.raises(ValueError, match="schema_name"):
        quote_table_ref("postgres", {"schema_name": "public orders", "table": "t"})


def test_quote_table_ref_rejects_space_in_table():
    """Security: space in table name fails identifier regex → ValueError."""
    with pytest.raises(ValueError, match="table"):
        quote_table_ref("postgres", {"schema_name": "public", "table": "order items"})


def test_quote_table_ref_rejects_over_63_char_schema():
    """Security: schema_name exceeding 63 chars (PostgreSQL NAMEDATALEN-1) → ValueError."""
    long_name = "a" * 64
    with pytest.raises(ValueError, match="schema_name"):
        quote_table_ref("postgres", {"schema_name": long_name, "table": "t"})


def test_quote_table_ref_rejects_over_63_char_table():
    """Security: table name exceeding 63 chars → ValueError."""
    long_name = "a" * 64
    with pytest.raises(ValueError, match="table"):
        quote_table_ref("postgres", {"schema_name": "s", "table": long_name})


def test_quote_table_ref_rejects_leading_digit_schema():
    """Security: schema name starting with digit fails [A-Za-z_] start rule → ValueError."""
    with pytest.raises(ValueError, match="schema_name"):
        quote_table_ref("postgres", {"schema_name": "1schema", "table": "t"})


def test_quote_table_ref_rejects_leading_digit_table():
    """Security: table name starting with digit → ValueError."""
    with pytest.raises(ValueError, match="table"):
        quote_table_ref("postgres", {"schema_name": "s", "table": "1table"})


# ── quote_table_ref — valid identifiers ──────────────────────────────────────


def test_quote_table_ref_accepts_valid_identifiers():
    """quote_table_ref produces \"schema\".\"table\" with double-quote wrapping."""
    ref = quote_table_ref("postgres", {"schema_name": "public", "table": "orders"})
    assert ref == '"public"."orders"'


def test_quote_table_ref_accepts_underscore_prefix():
    """Identifiers starting with underscore are valid per [A-Za-z_] rule."""
    ref = quote_table_ref("postgres", {"schema_name": "_private", "table": "_raw_events"})
    assert ref == '"_private"."_raw_events"'


def test_quote_table_ref_accepts_63_char_identifier():
    """63-char identifier is exactly at the cap — must be accepted."""
    name_63 = "a" * 63
    ref = quote_table_ref("postgres", {"schema_name": name_63, "table": "t"})
    assert f'"{name_63}"' in ref


def test_quote_table_ref_accepts_alphanumeric_with_underscores():
    """Mixed alphanumeric+underscore identifiers are valid."""
    ref = quote_table_ref("postgres", {"schema_name": "orders_2025", "table": "daily_fulfillment_summary"})
    assert ref == '"orders_2025"."daily_fulfillment_summary"'


def test_quote_table_ref_accepts_schema_key_alias():
    """quote_table_ref also accepts 'schema' key alias for schema_name."""
    ref = quote_table_ref("postgres", {"schema": "public", "table": "orders"})
    assert '"public"."orders"' == ref


# ── _IDENTIFIER_RE anchor tests ───────────────────────────────────────────────


def test_identifier_re_anchored_rejects_newline():
    """Defense-in-depth: _IDENTIFIER_RE with \\A/\\Z anchors must NOT match strings with newlines."""
    # Without \\A/\\Z anchors, re.match("^...$") would match the first line only
    # With \\A/\\Z, the entire string must match — newline terminates the match
    assert _IDENTIFIER_RE.match("foo\n") is None, (
        "_IDENTIFIER_RE must use \\A/\\Z anchors to reject 'foo\\n' (not ^/$ which allow newlines)"
    )


def test_identifier_re_anchored_rejects_newline_in_middle():
    """_IDENTIFIER_RE rejects identifier with embedded newline."""
    assert _IDENTIFIER_RE.match("foo\nbar") is None


def test_identifier_re_matches_valid_identifier():
    """_IDENTIFIER_RE correctly matches valid SQL identifiers."""
    assert _IDENTIFIER_RE.match("valid_column_name") is not None
    assert _IDENTIFIER_RE.match("_private") is not None
    assert _IDENTIFIER_RE.match("Column1") is not None


def test_identifier_re_rejects_empty_string():
    """_IDENTIFIER_RE rejects empty string."""
    assert _IDENTIFIER_RE.match("") is None


def test_identifier_re_rejects_leading_digit():
    """_IDENTIFIER_RE rejects identifiers starting with a digit."""
    assert _IDENTIFIER_RE.match("1bad") is None


def test_identifier_re_rejects_over_63_chars():
    """_IDENTIFIER_RE rejects identifiers longer than 63 chars."""
    assert _IDENTIFIER_RE.match("a" * 64) is None


def test_identifier_re_accepts_exactly_63_chars():
    """_IDENTIFIER_RE accepts exactly 63-char identifier (NAMEDATALEN-1)."""
    assert _IDENTIFIER_RE.match("a" * 63) is not None


# ── _resolve_postgresql_password ──────────────────────────────────────────────


def test_resolve_postgresql_password_calls_resolve_secret_ref_for_dict_secret_ref():
    """_resolve_postgresql_password calls resolve_secret_ref when auth.secret_ref is a dict."""
    auth = {
        "username": "etl",
        "secret_ref": {"name": "imazon-db-cred", "key": "password"},
    }

    with patch(
        "src.backend.ingestion.secret_resolver.resolve_secret_ref",
        return_value="resolved-password-xyz",
    ) as mock_resolve:
        password = _resolve_postgresql_password(auth)

    mock_resolve.assert_called_once_with("k8s-secret/imazon-db-cred/password")
    assert password == "resolved-password-xyz"


def test_resolve_postgresql_password_returns_resolved_not_dict():
    """Security: password must be the resolved string, not the secret_ref dict object."""
    auth = {
        "username": "etl",
        "secret_ref": {"name": "secret", "key": "pw"},
    }

    with patch(
        "src.backend.ingestion.secret_resolver.resolve_secret_ref",
        return_value="plaintext-pw",
    ):
        password = _resolve_postgresql_password(auth)

    # Old buggy code returned the dict; new code must return the string
    assert isinstance(password, str), "Password must be a string, not a dict"
    assert password == "plaintext-pw"


def test_resolve_postgresql_password_returns_empty_when_no_auth():
    """_resolve_postgresql_password returns empty string when auth is None."""
    result = _resolve_postgresql_password(None)
    assert result == ""


def test_resolve_postgresql_password_returns_empty_when_no_secret_ref():
    """_resolve_postgresql_password returns empty string when auth has no secret_ref."""
    result = _resolve_postgresql_password({"username": "etl"})
    assert result == ""


def test_resolve_postgresql_password_raises_when_secret_ref_is_string():
    """_resolve_postgresql_password raises ValueError when secret_ref is a plain string (wrong shape)."""
    auth = {"secret_ref": "not-a-dict"}
    with pytest.raises(ValueError, match="invalid secret_ref shape"):
        _resolve_postgresql_password(auth)
