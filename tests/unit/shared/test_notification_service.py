"""Unit tests for NotificationService."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from src.shared.exceptions import NotificationError
from src.shared.notifications.config import NotificationSettings
from src.shared.notifications.models import ActionItem, SLAAlert
from src.shared.notifications.service import NotificationService

NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)


def _disabled_settings() -> NotificationSettings:
    return NotificationSettings(notification_enabled=False)


def _enabled_settings() -> NotificationSettings:
    return NotificationSettings(
        notification_enabled=True,
        smtp_host="smtp.example.com",
        smtp_port=587,
        smtp_user="user@example.com",
        smtp_password="secret",
        notification_from="dataspoke@example.com",
    )


def _sample_items() -> list[ActionItem]:
    return [
        ActionItem(
            dataset_urn="urn:li:dataset:tbl_a",
            issue_type="missing_owner",
            priority="high",
            description="No owner assigned",
            estimated_fix_minutes=5,
            projected_score_impact=10.0,
        ),
        ActionItem(
            dataset_urn="urn:li:dataset:tbl_b",
            issue_type="stale",
            priority="critical",
            description="Not refreshed",
            estimated_fix_minutes=30,
            projected_score_impact=15.0,
        ),
    ]


def _sample_alert() -> SLAAlert:
    return SLAAlert(
        dataset_urn="urn:li:dataset:tbl_a",
        sla_name="freshness_4h",
        predicted_breach_at=NOW,
        root_cause="Upstream delayed",
        recommended_actions=["Check DAG", "Contact on-call"],
    )


# ── No-op mode ───────────────────────────────────────────────────────────────


async def test_send_email_noop_when_disabled() -> None:
    svc = NotificationService(settings=_disabled_settings())
    with patch("src.shared.notifications.service.logger") as mock_logger:
        await svc.send_email(["a@b.com"], "Subject", "<p>hi</p>")
        mock_logger.info.assert_called_once()
        call_kwargs = mock_logger.info.call_args
        assert call_kwargs[0][0] == "notification_noop"
        assert call_kwargs[1]["recipients"] == ["a@b.com"]
        assert call_kwargs[1]["subject"] == "Subject"


async def test_send_action_items_noop_when_disabled() -> None:
    svc = NotificationService(settings=_disabled_settings())
    with patch("src.shared.notifications.service.logger") as mock_logger:
        await svc.send_action_items("owner@x.com", _sample_items())
        mock_logger.info.assert_called_once()
        assert mock_logger.info.call_args[1]["action"] == "send_action_items"


async def test_send_sla_alert_noop_when_disabled() -> None:
    svc = NotificationService(settings=_disabled_settings())
    with patch("src.shared.notifications.service.logger") as mock_logger:
        await svc.send_sla_alert(["a@b.com"], _sample_alert())
        mock_logger.info.assert_called_once()
        assert mock_logger.info.call_args[1]["action"] == "send_sla_alert"


async def test_send_alarm_noop_when_disabled() -> None:
    svc = NotificationService(settings=_disabled_settings())
    with patch("src.shared.notifications.service.logger") as mock_logger:
        await svc.send_alarm(["a@b.com"], "row_count", 50.0, 100.0)
        mock_logger.info.assert_called_once()
        assert mock_logger.info.call_args[1]["action"] == "send_alarm"


# ── Email body formatting ────────────────────────────────────────────────────


async def test_send_action_items_formats_html() -> None:
    svc = NotificationService(settings=_enabled_settings())
    svc.send_email = AsyncMock()  # type: ignore[method-assign]

    await svc.send_action_items("owner@x.com", _sample_items())

    svc.send_email.assert_awaited_once()
    call_args = svc.send_email.call_args
    body = call_args[1]["body_html"] if "body_html" in (call_args[1] or {}) else call_args[0][2]
    assert "urn:li:dataset:tbl_a" in body
    assert "urn:li:dataset:tbl_b" in body
    assert "missing_owner" in body
    assert "No owner assigned" in body
    assert "10.0" in body
    assert "15.0" in body


async def test_send_action_items_sorts_by_priority() -> None:
    svc = NotificationService(settings=_enabled_settings())
    svc.send_email = AsyncMock()  # type: ignore[method-assign]

    await svc.send_action_items("owner@x.com", _sample_items())

    body = svc.send_email.call_args[1]["body_html"]
    # "critical" should appear before "high" in the rendered HTML
    assert body.index("critical") < body.index("high")


async def test_send_sla_alert_formats_html() -> None:
    svc = NotificationService(settings=_enabled_settings())
    svc.send_email = AsyncMock()  # type: ignore[method-assign]

    await svc.send_sla_alert(["a@b.com"], _sample_alert())

    body = svc.send_email.call_args[1]["body_html"]
    assert "urn:li:dataset:tbl_a" in body
    assert "freshness_4h" in body
    assert "Upstream delayed" in body
    assert "Check DAG" in body
    assert "Contact on-call" in body
    assert NOW.isoformat() in body


async def test_send_alarm_formats_html() -> None:
    svc = NotificationService(settings=_enabled_settings())
    svc.send_email = AsyncMock()  # type: ignore[method-assign]

    await svc.send_alarm(["a@b.com"], "row_count", 50.0, 100.0)

    body = svc.send_email.call_args[1]["body_html"]
    assert "row_count" in body
    assert "50.0" in body
    assert "100.0" in body


# ── SMTP integration (mocked) ────────────────────────────────────────────────


async def test_send_email_connects_and_sends() -> None:
    svc = NotificationService(settings=_enabled_settings())

    mock_smtp_instance = AsyncMock()
    with patch("src.shared.notifications.service.aiosmtplib.SMTP") as mock_smtp_cls:
        mock_smtp_cls.return_value = mock_smtp_instance

        await svc.send_email(["user@example.com"], "Test", "<p>body</p>")

        mock_smtp_cls.assert_called_once_with(hostname="smtp.example.com", port=587)
        mock_smtp_instance.connect.assert_awaited_once()
        mock_smtp_instance.starttls.assert_awaited_once()
        mock_smtp_instance.login.assert_awaited_once_with("user@example.com", "secret")
        mock_smtp_instance.sendmail.assert_awaited_once()
        mock_smtp_instance.quit.assert_awaited_once()

        # Verify sendmail args
        sendmail_args = mock_smtp_instance.sendmail.call_args[0]
        assert sendmail_args[0] == "dataspoke@example.com"
        assert sendmail_args[1] == ["user@example.com"]


async def test_send_email_no_auth_when_no_credentials() -> None:
    settings = NotificationSettings(
        notification_enabled=True,
        smtp_host="smtp.example.com",
        smtp_user="",
        smtp_password="",
    )
    svc = NotificationService(settings=settings)

    mock_smtp_instance = AsyncMock()
    with patch("src.shared.notifications.service.aiosmtplib.SMTP") as mock_smtp_cls:
        mock_smtp_cls.return_value = mock_smtp_instance

        await svc.send_email(["user@example.com"], "Test", "<p>body</p>")

        mock_smtp_instance.login.assert_not_awaited()
        mock_smtp_instance.sendmail.assert_awaited_once()


async def test_send_email_raises_notification_error_on_smtp_failure() -> None:
    svc = NotificationService(settings=_enabled_settings())

    mock_smtp_instance = AsyncMock()
    mock_smtp_instance.connect.side_effect = ConnectionRefusedError("Connection refused")

    with patch("src.shared.notifications.service.aiosmtplib.SMTP") as mock_smtp_cls:
        mock_smtp_cls.return_value = mock_smtp_instance

        with pytest.raises(NotificationError, match="Failed to send email"):
            await svc.send_email(["user@example.com"], "Test", "<p>body</p>")


# ── Edge cases ────────────────────────────────────────────────────────────────


async def test_send_action_items_empty_list() -> None:
    svc = NotificationService(settings=_enabled_settings())
    svc.send_email = AsyncMock()  # type: ignore[method-assign]

    with patch("src.shared.notifications.service.logger") as mock_logger:
        await svc.send_action_items("owner@x.com", [])
        svc.send_email.assert_not_awaited()
        mock_logger.info.assert_called_once()
