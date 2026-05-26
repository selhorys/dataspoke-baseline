"""Async notification service with email delivery backed by the SMTP peripheral.

SMTP connection settings are read from the ``peripheral_config`` DB table at
send time (via ``peripheral_service.get_peripheral_config(db, 'smtp')``).
The SMTP password is read from the ``dataspoke-smtp-secret`` Kubernetes Secret
via ``smtp_secret.get_smtp_password()``.

When the peripheral is unconfigured (no DB row, or host/from_address empty, or
password unset), ``send_email`` raises ``PeripheralNotConfiguredError('smtp')``.
Callers decide whether to propagate or swallow the error:

- ``reset.issue_reset_token``   — propagates (password reset requires SMTP).
- ``send_action_items``         — swallows + logs (owner digests are best-effort).
- ``send_sla_alert``            — swallows + logs.
- ``send_alarm``                — swallows + logs.
"""

from __future__ import annotations

import html
import logging
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

import aiosmtplib
import structlog

from src.shared.exceptions import NotificationError, PeripheralNotConfiguredError
from src.shared.notifications.models import ActionItem, SLAAlert

_PRIORITY_ORDER = {"critical": 0, "high": 1, "medium": 2}

logger = structlog.get_logger(__name__)
_stdlib_logger = logging.getLogger(__name__)


class NotificationService:
    """Async notification service supporting email delivery.

    Reads SMTP config from the peripheral DB row and Kubernetes Secret at
    send time.  Raises ``PeripheralNotConfiguredError('smtp')`` when SMTP is
    not fully configured.

    Args:
        db_session_factory: Callable that returns an async context-manager
            yielding an AsyncSession (e.g. ``src.shared.db.session.SessionLocal``).
    """

    def __init__(
        self,
        db_session_factory: Callable[..., Any],
    ) -> None:
        self._db_session_factory = db_session_factory

    async def send_email(self, to: list[str], subject: str, body_html: str) -> None:
        """Send an HTML email to the given recipients.

        Raises:
            PeripheralNotConfiguredError('smtp')  — SMTP not configured.
            NotificationError                     — SMTP transport failure.
        """
        from src.backend.admin import peripheral_service, smtp_secret
        from src.backend.admin.peripheral_service import SmtpConfigDTO

        async with self._db_session_factory() as db:
            dto = await peripheral_service.get_peripheral_config(db, "smtp")

        password = smtp_secret.get_smtp_password()

        if (
            dto is None
            or not isinstance(dto, SmtpConfigDTO)
            or not dto.host
            or not dto.from_address
            or not password
        ):
            logger.info(
                "smtp_peripheral_unconfigured",
                action="send_email",
                recipients=to,
                subject=subject,
            )
            raise PeripheralNotConfiguredError("smtp")

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = dto.from_address
        msg["To"] = ", ".join(to)
        msg.attach(MIMEText(body_html, "html"))

        try:
            smtp = aiosmtplib.SMTP(hostname=dto.host, port=dto.port)
            await smtp.connect()
            if dto.use_tls:
                await smtp.starttls()
            if dto.username:
                await smtp.login(dto.username, password)
            await smtp.sendmail(dto.from_address, to, msg.as_string())
            await smtp.quit()
        except Exception as exc:
            _stdlib_logger.error(
                "smtp_send_failed", extra={"recipients": to, "subject": subject, "error": str(exc)}
            )
            raise NotificationError(f"Failed to send email: {exc}") from exc

    async def send_action_items(self, owner_email: str, items: list[ActionItem]) -> None:
        """Send an action-item digest email to a dataset owner.

        Silently no-ops (log at INFO) when SMTP is not configured, preserving
        the existing best-effort semantics for owner digests.
        """
        if not items:
            _stdlib_logger.info("send_action_items_empty", extra={"owner": owner_email})
            return

        sorted_items = sorted(items, key=lambda i: _PRIORITY_ORDER.get(i.priority, 99))

        rows = []
        for item in sorted_items:
            rows.append(
                "<tr>"
                f"<td>{html.escape(item.dataset_urn)}</td>"
                f"<td>{html.escape(item.priority)}</td>"
                f"<td>{html.escape(item.issue_type)}</td>"
                f"<td>{html.escape(item.description)}</td>"
                f"<td>{item.estimated_fix_minutes}</td>"
                f"<td>{item.projected_score_impact:.1f}</td>"
                "</tr>"
            )

        body = (
            "<h2>DataSpoke Action Items</h2>"
            "<table border='1' cellpadding='4'>"
            "<tr><th>Dataset</th><th>Priority</th><th>Issue</th>"
            "<th>Description</th><th>Est. Fix (min)</th><th>Score Impact</th></tr>"
            + "".join(rows)
            + "</table>"
        )

        try:
            await self.send_email(
                to=[owner_email],
                subject="DataSpoke: Action Items for Your Datasets",
                body_html=body,
            )
        except PeripheralNotConfiguredError:
            logger.info(
                "notification_noop",
                action="send_action_items",
                owner=owner_email,
                item_count=len(items),
            )

    async def send_sla_alert(self, recipients: list[str], alert: SLAAlert) -> None:
        """Send an SLA breach prediction alert.

        Silently no-ops when SMTP is not configured.
        """
        actions_html = "".join(f"<li>{html.escape(a)}</li>" for a in alert.recommended_actions)

        body = (
            "<h2>SLA Breach Alert</h2>"
            f"<p><strong>Dataset:</strong> {html.escape(alert.dataset_urn)}</p>"
            f"<p><strong>SLA:</strong> {html.escape(alert.sla_name)}</p>"
            f"<p><strong>Predicted Breach:</strong> {alert.predicted_breach_at.isoformat()}</p>"
            f"<p><strong>Root Cause:</strong> {html.escape(alert.root_cause)}</p>"
            "<p><strong>Recommended Actions:</strong></p>"
            f"<ul>{actions_html}</ul>"
        )

        try:
            await self.send_email(
                to=recipients,
                subject=f"DataSpoke SLA Alert: {alert.sla_name}",
                body_html=body,
            )
        except PeripheralNotConfiguredError:
            logger.info(
                "notification_noop",
                action="send_sla_alert",
                recipients=recipients,
                dataset_urn=alert.dataset_urn,
            )

    async def send_alarm(
        self,
        recipients: list[str],
        metric_id: str,
        value: float,
        threshold: float,
    ) -> None:
        """Send a metric alarm notification.

        Silently no-ops when SMTP is not configured.
        """
        body = (
            "<h2>Metric Alarm</h2>"
            f"<p><strong>Metric:</strong> {html.escape(metric_id)}</p>"
            f"<p><strong>Current Value:</strong> {value}</p>"
            f"<p><strong>Threshold:</strong> {threshold}</p>"
        )

        try:
            await self.send_email(
                to=recipients,
                subject=f"DataSpoke Alarm: {metric_id}",
                body_html=body,
            )
        except PeripheralNotConfiguredError:
            logger.info(
                "notification_noop",
                action="send_alarm",
                recipients=recipients,
                metric_id=metric_id,
                value=value,
                threshold=threshold,
            )
