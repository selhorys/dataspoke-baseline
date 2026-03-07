"""Unit tests for NotificationService — no real SMTP connection needed."""

from unittest.mock import MagicMock, patch

import pytest

from src.shared.notifications.service import NotificationService


def _make_service(enabled: bool = True) -> NotificationService:
    return NotificationService(
        enabled=enabled,
        smtp_host="smtp.example.com",
        smtp_port=587,
        smtp_user="user",
        smtp_password="pass",
        sender="test@example.com",
    )


@pytest.mark.asyncio
async def test_send_email_disabled_is_noop():
    service = _make_service(enabled=False)
    with patch("src.shared.notifications.service.smtplib.SMTP") as mock_smtp:
        await service.send_email(["to@example.com"], "Subject", "<p>body</p>")
        mock_smtp.assert_not_called()


@pytest.mark.asyncio
async def test_send_email_enabled():
    service = _make_service(enabled=True)
    with patch("src.shared.notifications.service.smtplib.SMTP") as mock_smtp:
        mock_server = MagicMock()
        mock_smtp.return_value.__enter__ = MagicMock(return_value=mock_server)
        mock_smtp.return_value.__exit__ = MagicMock(return_value=False)

        await service.send_email(["to@example.com"], "Hello", "<p>hi</p>")

        mock_smtp.assert_called_once_with("smtp.example.com", 587)
        mock_server.starttls.assert_called_once()
        mock_server.login.assert_called_once_with("user", "pass")
        mock_server.sendmail.assert_called_once()
        call_args = mock_server.sendmail.call_args
        assert call_args[0][0] == "test@example.com"
        assert call_args[0][1] == ["to@example.com"]


@pytest.mark.asyncio
async def test_send_email_no_auth_when_user_empty():
    service = NotificationService(
        enabled=True,
        smtp_host="smtp.example.com",
        smtp_port=25,
        smtp_user="",
        smtp_password="",
        sender="noreply@example.com",
    )
    with patch("src.shared.notifications.service.smtplib.SMTP") as mock_smtp:
        mock_server = MagicMock()
        mock_smtp.return_value.__enter__ = MagicMock(return_value=mock_server)
        mock_smtp.return_value.__exit__ = MagicMock(return_value=False)

        await service.send_email(["to@example.com"], "Test", "<p>test</p>")

        mock_server.starttls.assert_not_called()
        mock_server.login.assert_not_called()


@pytest.mark.asyncio
async def test_send_action_items_empty_is_noop():
    service = _make_service(enabled=True)
    with patch("src.shared.notifications.service.smtplib.SMTP") as mock_smtp:
        await service.send_action_items("owner@example.com", [])
        mock_smtp.assert_not_called()


@pytest.mark.asyncio
async def test_send_action_items_sends_email():
    service = _make_service(enabled=True)
    items = [{"issue_type": "missing_owner", "priority": "high", "description": "Add owner"}]
    with patch.object(service, "send_email") as mock_send:
        await service.send_action_items("owner@example.com", items)
        mock_send.assert_awaited_once()
        call_args = mock_send.call_args
        assert call_args[0][0] == ["owner@example.com"]
        assert "Action Items" in call_args[0][1]


@pytest.mark.asyncio
async def test_send_sla_alert():
    service = _make_service(enabled=True)
    alert = {"dataset_urn": "urn:li:dataset:test", "predicted_breach_time": "2h"}
    with patch.object(service, "send_email") as mock_send:
        await service.send_sla_alert(["admin@example.com"], alert)
        mock_send.assert_awaited_once()
        assert "SLA Alert" in mock_send.call_args[0][1]


@pytest.mark.asyncio
async def test_send_alarm():
    service = _make_service(enabled=True)
    with patch.object(service, "send_email") as mock_send:
        await service.send_alarm(["admin@example.com"], "freshness_score", 0.3, 0.5)
        mock_send.assert_awaited_once()
        assert "freshness_score" in mock_send.call_args[0][1]
