"""Tests for src/shared/notifications/service.py — NotificationService.

The NotificationService now reads SMTP config from the peripheral_config DB
row and the dataspoke-smtp-secret Kubernetes Secret at send time.

No-op mode is triggered by an unconfigured peripheral
(PeripheralNotConfiguredError), not by a settings flag.

SMTP dispatch is tested by mocking the peripheral lookup and aiosmtplib.SMTP.
"""

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.shared.exceptions import NotificationError, PeripheralNotConfiguredError
from src.shared.notifications.models import ActionItem, SLAAlert
from src.shared.notifications.service import NotificationService

NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_session_factory(dto=None):
    """Return a session factory that yields an AsyncSession mock with pre-loaded peripheral."""
    mock_db = AsyncMock()

    @asynccontextmanager
    async def _factory():
        yield mock_db

    return _factory


def _configured_dto():
    from src.backend.admin.peripheral_service import SmtpConfigDTO

    return SmtpConfigDTO(
        host="smtp.example.com",
        port=587,
        username="user@example.com",
        from_address="dataspoke@example.com",
        use_tls=True,
    )


def _unconfigured_dto():
    return None


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


def _make_svc(dto=None, password: str = "secret") -> NotificationService:
    """Return a NotificationService whose peripheral is pre-mocked."""
    factory = _make_session_factory(dto)
    return NotificationService(db_session_factory=factory)


# ── No-op mode (unconfigured peripheral) ─────────────────────────────────────


@pytest.mark.asyncio
async def test_send_email_noop_when_peripheral_unconfigured() -> None:
    svc = _make_svc()
    with (
        patch(
            "src.backend.admin.peripheral_service.get_peripheral_config",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "src.backend.admin.smtp_secret.get_smtp_password",
            return_value="",
        ),
        patch("src.shared.notifications.service.logger") as mock_logger,
    ):
        with pytest.raises(PeripheralNotConfiguredError):
            await svc.send_email(["a@b.com"], "Subject", "<p>hi</p>")
        mock_logger.info.assert_called_once()
        assert mock_logger.info.call_args[0][0] == "smtp_peripheral_unconfigured"


@pytest.mark.asyncio
async def test_send_action_items_noop_when_peripheral_unconfigured() -> None:
    svc = _make_svc()
    with (
        patch(
            "src.backend.admin.peripheral_service.get_peripheral_config",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "src.backend.admin.smtp_secret.get_smtp_password",
            return_value="",
        ),
        patch("src.shared.notifications.service.logger") as mock_logger,
    ):
        # send_action_items swallows PeripheralNotConfiguredError
        await svc.send_action_items("owner@x.com", _sample_items())
        mock_logger.info.assert_called()
        calls = [c[0][0] for c in mock_logger.info.call_args_list]
        assert "notification_noop" in calls


@pytest.mark.asyncio
async def test_send_sla_alert_noop_when_peripheral_unconfigured() -> None:
    svc = _make_svc()
    with (
        patch(
            "src.backend.admin.peripheral_service.get_peripheral_config",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "src.backend.admin.smtp_secret.get_smtp_password",
            return_value="",
        ),
        patch("src.shared.notifications.service.logger") as mock_logger,
    ):
        await svc.send_sla_alert(["a@b.com"], _sample_alert())
        mock_logger.info.assert_called()
        calls = [c[0][0] for c in mock_logger.info.call_args_list]
        assert "notification_noop" in calls


@pytest.mark.asyncio
async def test_send_alarm_noop_when_peripheral_unconfigured() -> None:
    svc = _make_svc()
    with (
        patch(
            "src.backend.admin.peripheral_service.get_peripheral_config",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "src.backend.admin.smtp_secret.get_smtp_password",
            return_value="",
        ),
        patch("src.shared.notifications.service.logger") as mock_logger,
    ):
        await svc.send_alarm(["a@b.com"], "row_count", 50.0, 100.0)
        mock_logger.info.assert_called()
        calls = [c[0][0] for c in mock_logger.info.call_args_list]
        assert "notification_noop" in calls


# ── Email body formatting ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_action_items_formats_html() -> None:
    svc = _make_svc()
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


@pytest.mark.asyncio
async def test_send_action_items_sorts_by_priority() -> None:
    svc = _make_svc()
    svc.send_email = AsyncMock()  # type: ignore[method-assign]

    await svc.send_action_items("owner@x.com", _sample_items())

    body = svc.send_email.call_args[1]["body_html"]
    # "critical" should appear before "high" in the rendered HTML
    assert body.index("critical") < body.index("high")


@pytest.mark.asyncio
async def test_send_sla_alert_formats_html() -> None:
    svc = _make_svc()
    svc.send_email = AsyncMock()  # type: ignore[method-assign]

    await svc.send_sla_alert(["a@b.com"], _sample_alert())

    body = svc.send_email.call_args[1]["body_html"]
    assert "urn:li:dataset:tbl_a" in body
    assert "freshness_4h" in body
    assert "Upstream delayed" in body
    assert "Check DAG" in body
    assert "Contact on-call" in body
    assert NOW.isoformat() in body


@pytest.mark.asyncio
async def test_send_alarm_formats_html() -> None:
    svc = _make_svc()
    svc.send_email = AsyncMock()  # type: ignore[method-assign]

    await svc.send_alarm(["a@b.com"], "row_count", 50.0, 100.0)

    body = svc.send_email.call_args[1]["body_html"]
    assert "row_count" in body
    assert "50.0" in body
    assert "100.0" in body


# ── SMTP integration (mocked) ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_email_connects_and_sends() -> None:
    svc = _make_svc()

    mock_smtp_instance = AsyncMock()
    with (
        patch(
            "src.backend.admin.peripheral_service.get_peripheral_config",
            new=AsyncMock(return_value=_configured_dto()),
        ),
        patch(
            "src.backend.admin.smtp_secret.get_smtp_password",
            return_value="secret",
        ),
        patch("src.shared.notifications.service.aiosmtplib.SMTP") as mock_smtp_cls,
    ):
        mock_smtp_cls.return_value = mock_smtp_instance

        await svc.send_email(["user@example.com"], "Test", "<p>body</p>")

        mock_smtp_cls.assert_called_once_with(hostname="smtp.example.com", port=587)
        mock_smtp_instance.connect.assert_awaited_once()
        mock_smtp_instance.starttls.assert_awaited_once()
        mock_smtp_instance.login.assert_awaited_once_with("user@example.com", "secret")
        mock_smtp_instance.sendmail.assert_awaited_once()
        mock_smtp_instance.quit.assert_awaited_once()

        sendmail_args = mock_smtp_instance.sendmail.call_args[0]
        assert sendmail_args[0] == "dataspoke@example.com"
        assert sendmail_args[1] == ["user@example.com"]


@pytest.mark.asyncio
async def test_send_email_no_auth_when_no_username() -> None:
    from src.backend.admin.peripheral_service import SmtpConfigDTO

    dto_no_user = SmtpConfigDTO(
        host="smtp.example.com",
        port=587,
        username="",
        from_address="dataspoke@example.com",
        use_tls=True,
    )

    svc = _make_svc()
    mock_smtp_instance = AsyncMock()

    with (
        patch(
            "src.backend.admin.peripheral_service.get_peripheral_config",
            new=AsyncMock(return_value=dto_no_user),
        ),
        patch(
            "src.backend.admin.smtp_secret.get_smtp_password",
            return_value="secret",
        ),
        patch("src.shared.notifications.service.aiosmtplib.SMTP") as mock_smtp_cls,
    ):
        mock_smtp_cls.return_value = mock_smtp_instance

        await svc.send_email(["user@example.com"], "Test", "<p>body</p>")

        mock_smtp_instance.login.assert_not_awaited()
        mock_smtp_instance.sendmail.assert_awaited_once()


@pytest.mark.asyncio
async def test_send_email_raises_notification_error_on_smtp_failure() -> None:
    svc = _make_svc()

    mock_smtp_instance = AsyncMock()
    mock_smtp_instance.connect.side_effect = ConnectionRefusedError("Connection refused")

    with (
        patch(
            "src.backend.admin.peripheral_service.get_peripheral_config",
            new=AsyncMock(return_value=_configured_dto()),
        ),
        patch(
            "src.backend.admin.smtp_secret.get_smtp_password",
            return_value="secret",
        ),
        patch("src.shared.notifications.service.aiosmtplib.SMTP") as mock_smtp_cls,
    ):
        mock_smtp_cls.return_value = mock_smtp_instance

        with pytest.raises(NotificationError, match="Failed to send email"):
            await svc.send_email(["user@example.com"], "Test", "<p>body</p>")


# ── Edge cases ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_action_items_empty_list() -> None:
    svc = _make_svc()
    svc.send_email = AsyncMock()  # type: ignore[method-assign]

    await svc.send_action_items("owner@x.com", [])
    svc.send_email.assert_not_awaited()
