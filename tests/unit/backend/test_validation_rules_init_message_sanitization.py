"""Unit tests — catch-all evaluator message sanitization (Group A8).

Spec sources:
- spec/feature/BACKEND.md §Validation Service — "sanitized exception message in catch-all"
- src/backend/validation/rules/__init__.py evaluate_rule catch-all block
"""

import logging
from unittest.mock import AsyncMock, patch

import pytest

from src.backend.validation.rules import evaluate_rule

_DATASET_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,imazon.orders.daily_fulfillment_summary,DEV)"


# ── Catch-all sanitization ────────────────────────────────────────────────────


async def test_evaluator_exception_message_sanitized(datahub, caplog):
    """BACKEND.md catch-all: exception message in issues must NOT leak host/password/internal info."""

    class LeakyException(RuntimeError):
        pass

    async def mock_evaluate(dh, dataset_urn, rule, partition, db=None):
        raise LeakyException(
            "Connection failed: host=internal-db.cluster:5432 password={'name': 'secret', 'key': 'pw'}"
        )

    rule = {"rule_id": "r_leak", "type": "freshness", "lookback_interval": "24h"}

    # Patch get_evaluator at the module level where evaluate_rule looks it up.
    # evaluate_rule does: evaluator = get_evaluator(rule_type) or get_evaluator("custom")
    # We return mock_evaluate for any name so both branches resolve to our mock.
    with (
        patch("src.backend.validation.rules.get_evaluator", return_value=mock_evaluate),
        caplog.at_level(logging.WARNING, logger="src.backend.validation.rules"),
    ):
        result = await evaluate_rule(datahub, _DATASET_URN, rule, {})

    assert result.assertion_result == "ERROR"
    assert len(result.issues) > 0

    issue_msg = result.issues[0].get("msg", "")

    # The issue message must be sanitized — must contain the exception class name
    assert "LeakyException" in issue_msg, (
        f"Sanitized message should contain exception type name, got: {issue_msg!r}"
    )

    # Must NOT contain connection details
    assert "host=internal-db.cluster:5432" not in issue_msg, (
        f"Issue msg must not leak host: {issue_msg!r}"
    )
    assert "password" not in issue_msg, (
        f"Issue msg must not contain 'password': {issue_msg!r}"
    )


async def test_evaluator_exception_server_side_log_has_full_exception(datahub, caplog):
    """BACKEND.md catch-all: server-side log must contain inner exception text for operators."""

    class SignalException(ValueError):
        pass

    inner_msg = "signal-xyz internal detail"

    async def mock_evaluate(dh, dataset_urn, rule, partition, db=None):
        raise SignalException(inner_msg)

    rule = {"rule_id": "r_log_check", "type": "freshness"}

    with (
        patch("src.backend.validation.rules.get_evaluator", return_value=mock_evaluate),
        caplog.at_level(logging.WARNING, logger="src.backend.validation.rules"),
    ):
        result = await evaluate_rule(datahub, _DATASET_URN, rule, {})

    # Must have logged at WARNING level
    warning_records = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warning_records) > 0, "Catch-all must emit a WARNING-level log"

    # Spec invariant: server-side log carries the inner exception text somewhere.
    # Build combined string from message + full record dict (covers extra keys, exc_info, etc.)
    combined = " ".join(
        record.getMessage() + " " + str(record.__dict__)
        for record in caplog.records
    )
    assert inner_msg in combined, (
        "Server-side log must contain the inner exception detail for operator debugging"
    )


# F3: test_evaluator_error_issue_type_is_evaluation_error dropped — "evaluation_error"
# label is impl detail not found in spec. The sanitization test above already covers
# the catch-all path.


async def test_evaluator_error_message_contains_exception_type_name(datahub):
    """BACKEND.md catch-all: sanitized message must contain the exception class name
    and must NOT contain the exception's str() value (no-leak + traceability invariant).

    Note: the exact prefix format ('evaluator failed: ', etc.) is an impl/copy choice
    and is not mandated by spec. Spec only requires: class name present + no value leak.
    """

    class SpecificError(KeyError):
        pass

    async def mock_evaluate(dh, dataset_urn, rule, partition, db=None):
        raise SpecificError("secret-key-name")

    rule = {"rule_id": "r_type", "type": "freshness"}

    with patch("src.backend.validation.rules.get_evaluator", return_value=mock_evaluate):
        result = await evaluate_rule(datahub, _DATASET_URN, rule, {})

    issue_msg = result.issues[0].get("msg", "") if result.issues else ""
    # Spec invariant: must contain the exception class name
    assert "SpecificError" in issue_msg, (
        f"Sanitized message must include exception type name, got: {issue_msg!r}"
    )
    # Must NOT contain the actual key value (which could be sensitive)
    assert "secret-key-name" not in issue_msg, (
        f"Sanitized message must not contain exception value: {issue_msg!r}"
    )
