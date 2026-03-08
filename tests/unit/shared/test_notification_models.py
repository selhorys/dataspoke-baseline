"""Unit tests for notification domain models."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from src.shared.notifications.models import ActionItem, SLAAlert

NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)


# ── ActionItem ───────────────────────────────────────────────────────────────


def test_action_item_required_fields() -> None:
    item = ActionItem(
        dataset_urn="urn:li:dataset:(urn:li:dataPlatform:snowflake,db.schema.tbl,PROD)",
        issue_type="missing_owner",
        priority="critical",
        description="Dataset has no assigned owner",
        estimated_fix_minutes=5,
        projected_score_impact=12.5,
    )
    data = item.model_dump()
    assert data["dataset_urn"].startswith("urn:")
    assert data["issue_type"] == "missing_owner"
    assert data["priority"] == "critical"
    assert isinstance(data["estimated_fix_minutes"], int)
    assert isinstance(data["projected_score_impact"], float)

    # Round-trip
    restored = ActionItem.model_validate(data)
    assert restored == item


def test_action_item_optional_due_date() -> None:
    item = ActionItem(
        dataset_urn="urn:x",
        issue_type="stale",
        priority="medium",
        description="Not refreshed in 7 days",
        estimated_fix_minutes=30,
        projected_score_impact=8.0,
    )
    assert item.due_date is None


def test_action_item_with_due_date() -> None:
    item = ActionItem(
        dataset_urn="urn:x",
        issue_type="no_description",
        priority="high",
        description="Missing documentation",
        estimated_fix_minutes=10,
        projected_score_impact=5.0,
        due_date=NOW,
    )
    assert item.due_date == NOW


def test_action_item_rejects_missing_fields() -> None:
    with pytest.raises(ValidationError):
        ActionItem(
            dataset_urn="urn:x",
            # missing issue_type, priority, description, etc.
        )  # type: ignore[call-arg]


# ── SLAAlert ─────────────────────────────────────────────────────────────────


def test_sla_alert_serialization() -> None:
    alert = SLAAlert(
        dataset_urn="urn:li:dataset:(urn:li:dataPlatform:snowflake,db.schema.tbl,PROD)",
        sla_name="freshness_4h",
        predicted_breach_at=NOW,
        root_cause="Upstream pipeline delayed by 2 hours",
        recommended_actions=["Check Airflow DAG", "Contact data-eng on-call"],
    )
    data = alert.model_dump()
    assert data["dataset_urn"].startswith("urn:")
    assert data["sla_name"] == "freshness_4h"
    assert data["predicted_breach_at"] == NOW
    assert data["root_cause"].startswith("Upstream")
    assert len(data["recommended_actions"]) == 2


def test_sla_alert_recommended_actions_list() -> None:
    alert = SLAAlert(
        dataset_urn="urn:x",
        sla_name="completeness",
        predicted_breach_at=NOW,
        root_cause="Missing columns",
        recommended_actions=["Fix schema", "Re-run ingestion", "Validate output"],
    )
    assert isinstance(alert.recommended_actions, list)
    assert len(alert.recommended_actions) == 3


def test_sla_alert_empty_recommended_actions() -> None:
    alert = SLAAlert(
        dataset_urn="urn:x",
        sla_name="freshness",
        predicted_breach_at=NOW,
        root_cause="Unknown",
        recommended_actions=[],
    )
    assert alert.recommended_actions == []
