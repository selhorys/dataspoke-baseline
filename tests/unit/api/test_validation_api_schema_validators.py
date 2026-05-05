"""Unit tests — API schema validators for validation config (Group A9).

Spec sources:
- spec/feature/BACKEND.md §Validation Service "Source discriminator" table
- src/api/schemas/validation.py _validate_rules_source + _IDENTIFIER_RE
"""

import pytest
from pydantic import ValidationError

from src.api.schemas.validation import (
    CreateValidationConfigRequest,
    PatchValidationConfigRequest,
)

# ── CreateValidationConfigRequest — freshness.source ─────────────────────────


def test_create_accepts_freshness_source_datahub_operation():
    """API schema: freshness source=datahub_operation is valid."""
    req = CreateValidationConfigRequest(
        rules=[{
            "rule_id": "r1",
            "type": "freshness",
            "source": "datahub_operation",
            "lookback_interval": "24h",
        }],
        schedule_tier=None,
        is_enabled=False,
        owner="de@imazon.com",
    )
    assert req.rules[0]["source"] == "datahub_operation"


def test_create_accepts_freshness_source_datahub_profile():
    """API schema: freshness source=datahub_profile is valid."""
    req = CreateValidationConfigRequest(
        rules=[{
            "rule_id": "r1",
            "type": "freshness",
            "source": "datahub_profile",
            "lookback_interval": "24h",
        }],
        schedule_tier=None,
        is_enabled=False,
        owner="de@imazon.com",
    )
    assert req.rules[0]["source"] == "datahub_profile"


def test_create_accepts_freshness_source_query_with_valid_field():
    """API schema: freshness source=query with valid last_modified_field is accepted."""
    req = CreateValidationConfigRequest(
        rules=[{
            "rule_id": "r1",
            "type": "freshness",
            "source": "query",
            "lookback_interval": "24h",
            "last_modified_field": "updated_at",
        }],
        schedule_tier=None,
        is_enabled=False,
        owner="de@imazon.com",
    )
    assert req.rules[0]["source"] == "query"


def test_create_rejects_invalid_freshness_source():
    """API schema: unknown freshness source → 422."""
    with pytest.raises(ValidationError, match="source"):
        CreateValidationConfigRequest(
            rules=[{
                "rule_id": "r1",
                "type": "freshness",
                "source": "bogus",
                "lookback_interval": "24h",
            }],
            schedule_tier=None,
            is_enabled=False,
            owner="de@imazon.com",
        )


def test_create_rejects_native_as_freshness_source():
    """API schema: source='native' is rejected because it is not in the allowed enum
    {datahub_operation, datahub_profile, query}. It is a plausible-sounding value
    (mirrors assertionInfo.source.type=NATIVE at the DataHub layer) but the freshness
    source discriminator only accepts the three values above — 'native' fails the same
    enum check as any other unknown value.
    """
    with pytest.raises(ValidationError):
        CreateValidationConfigRequest(
            rules=[{
                "rule_id": "r1",
                "type": "freshness",
                "source": "native",
                "lookback_interval": "24h",
            }],
            schedule_tier=None,
            is_enabled=False,
            owner="de@imazon.com",
        )


def test_create_rejects_freshness_source_query_without_last_modified_field():
    """API schema: freshness source=query without last_modified_field → 422."""
    with pytest.raises(ValidationError, match="last_modified_field"):
        CreateValidationConfigRequest(
            rules=[{
                "rule_id": "r1",
                "type": "freshness",
                "source": "query",
                "lookback_interval": "24h",
                # no last_modified_field
            }],
            schedule_tier=None,
            is_enabled=False,
            owner="de@imazon.com",
        )


def test_create_rejects_freshness_source_query_with_sql_injection_field():
    """API schema: last_modified_field with SQL metacharacters → 422 before service call."""
    with pytest.raises(ValidationError, match="last_modified_field|identifier"):
        CreateValidationConfigRequest(
            rules=[{
                "rule_id": "r1",
                "type": "freshness",
                "source": "query",
                "lookback_interval": "24h",
                "last_modified_field": "updated_at; DROP TABLE orders",
            }],
            schedule_tier=None,
            is_enabled=False,
            owner="de@imazon.com",
        )


def test_create_rejects_freshness_source_query_with_newline_in_field():
    """API schema: newline in last_modified_field fails \\A/\\Z-anchored regex → 422."""
    with pytest.raises(ValidationError, match="last_modified_field|identifier"):
        CreateValidationConfigRequest(
            rules=[{
                "rule_id": "r1",
                "type": "freshness",
                "source": "query",
                "lookback_interval": "24h",
                "last_modified_field": "foo\n",
            }],
            schedule_tier=None,
            is_enabled=False,
            owner="de@imazon.com",
        )


def test_create_rejects_freshness_source_query_with_leading_digit_field():
    """API schema: last_modified_field starting with digit → 422."""
    with pytest.raises(ValidationError):
        CreateValidationConfigRequest(
            rules=[{
                "rule_id": "r1",
                "type": "freshness",
                "source": "query",
                "lookback_interval": "24h",
                "last_modified_field": "1bad_field",
            }],
            schedule_tier=None,
            is_enabled=False,
            owner="de@imazon.com",
        )


def test_create_rejects_freshness_source_query_with_over_63_char_field():
    """API schema: last_modified_field > 63 chars → 422 (NAMEDATALEN-1 cap)."""
    with pytest.raises(ValidationError):
        CreateValidationConfigRequest(
            rules=[{
                "rule_id": "r1",
                "type": "freshness",
                "source": "query",
                "lookback_interval": "24h",
                "last_modified_field": "a" * 64,
            }],
            schedule_tier=None,
            is_enabled=False,
            owner="de@imazon.com",
        )


# ── CreateValidationConfigRequest — volume.source ────────────────────────────


def test_create_accepts_volume_source_datahub_profile():
    """API schema: volume source=datahub_profile is valid."""
    req = CreateValidationConfigRequest(
        rules=[{
            "rule_id": "r1",
            "type": "volume",
            "source": "datahub_profile",
            "condition": {"type": "greater_than", "value": 100},
        }],
        schedule_tier=None,
        is_enabled=False,
        owner="de@imazon.com",
    )
    assert req.rules[0]["source"] == "datahub_profile"


def test_create_accepts_volume_source_query():
    """API schema: volume source=query is valid."""
    req = CreateValidationConfigRequest(
        rules=[{
            "rule_id": "r1",
            "type": "volume",
            "source": "query",
            "condition": {"type": "greater_than", "value": 0},
        }],
        schedule_tier=None,
        is_enabled=False,
        owner="de@imazon.com",
    )
    assert req.rules[0]["source"] == "query"


def test_create_rejects_volume_source_datahub_operation():
    """API schema: volume source=datahub_operation is NOT valid → 422."""
    with pytest.raises(ValidationError, match="source"):
        CreateValidationConfigRequest(
            rules=[{
                "rule_id": "r1",
                "type": "volume",
                "source": "datahub_operation",
                "condition": {"type": "greater_than", "value": 0},
            }],
            schedule_tier=None,
            is_enabled=False,
            owner="de@imazon.com",
        )


# ── source field rejected on non-freshness/volume types ──────────────────────


def test_create_rejects_source_on_field_rule():
    """API schema: source field is reserved for freshness/volume only — field rule → 422."""
    with pytest.raises(ValidationError, match="source"):
        CreateValidationConfigRequest(
            rules=[{
                "rule_id": "r1",
                "type": "field",
                "field": "col",
                "source": "query",
                "condition": {"type": "less_than_or_equal_to", "value": 0},
            }],
            schedule_tier=None,
            is_enabled=False,
            owner="de@imazon.com",
        )


def test_create_rejects_source_on_schema_rule():
    """API schema: source field on schema rule → 422."""
    with pytest.raises(ValidationError, match="source"):
        CreateValidationConfigRequest(
            rules=[{
                "rule_id": "r1",
                "type": "schema",
                "fields": [],
                "source": "query",
            }],
            schedule_tier=None,
            is_enabled=False,
            owner="de@imazon.com",
        )


def test_create_rejects_source_on_sql_rule():
    """API schema: source field on sql rule → 422."""
    with pytest.raises(ValidationError, match="source"):
        CreateValidationConfigRequest(
            rules=[{
                "rule_id": "r1",
                "type": "sql",
                "statement": "SELECT 1",
                "source": "query",
            }],
            schedule_tier=None,
            is_enabled=False,
            owner="de@imazon.com",
        )


def test_create_rejects_source_on_custom_rule():
    """API schema: source field on custom rule → 422."""
    with pytest.raises(ValidationError, match="source"):
        CreateValidationConfigRequest(
            rules=[{
                "rule_id": "r1",
                "type": "custom",
                "subtype": "sql_timeseries",
                "source": "query",
            }],
            schedule_tier=None,
            is_enabled=False,
            owner="de@imazon.com",
        )


# ── PatchValidationConfigRequest — rules validation ──────────────────────────


def test_patch_validates_rules_source_when_present():
    """API schema: PatchValidationConfigRequest applies same source validation when rules is present."""
    with pytest.raises(ValidationError, match="source"):
        PatchValidationConfigRequest(
            rules=[{
                "rule_id": "r1",
                "type": "freshness",
                "source": "bogus",
            }],
        )


def test_patch_accepts_none_rules():
    """API schema: PatchValidationConfigRequest with rules=None is valid (None-tolerant)."""
    req = PatchValidationConfigRequest(rules=None)
    assert req.rules is None


def test_patch_accepts_absent_rules():
    """API schema: PatchValidationConfigRequest without rules key is valid."""
    req = PatchValidationConfigRequest(is_enabled=False)
    assert req.rules is None


def test_patch_accepts_valid_rules():
    """API schema: PatchValidationConfigRequest with valid rules passes validation."""
    req = PatchValidationConfigRequest(
        rules=[{
            "rule_id": "r1",
            "type": "freshness",
            "source": "datahub_operation",
        }],
    )
    assert len(req.rules) == 1


def test_patch_rejects_invalid_freshness_source_when_rules_present():
    """API schema: PatchValidationConfigRequest applies freshness source validation."""
    with pytest.raises(ValidationError):
        PatchValidationConfigRequest(
            rules=[{
                "rule_id": "r1",
                "type": "freshness",
                "source": "datahub_operation",  # valid for freshness
            }, {
                "rule_id": "r2",
                "type": "volume",
                "source": "datahub_operation",  # INVALID for volume
            }],
        )
