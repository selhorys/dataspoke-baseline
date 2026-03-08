"""Async notification service with email delivery."""

import html
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import aiosmtplib
import structlog

from src.shared.exceptions import NotificationError
from src.shared.notifications.config import NotificationSettings, notification_settings
from src.shared.notifications.models import ActionItem, SLAAlert

_PRIORITY_ORDER = {"critical": 0, "high": 1, "medium": 2}

logger = structlog.get_logger(__name__)


class NotificationService:
    """Async notification service supporting email delivery.

    Operates in no-op mode by default (notification_enabled=False),
    logging notifications instead of sending them.
    """

    def __init__(self, settings: NotificationSettings | None = None) -> None:
        self._settings = settings or notification_settings

    async def send_email(self, to: list[str], subject: str, body_html: str) -> None:
        """Send an HTML email to the given recipients."""
        if not self._settings.notification_enabled:
            logger.info(
                "notification_noop",
                action="send_email",
                recipients=to,
                subject=subject,
            )
            return

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = self._settings.notification_from
        msg["To"] = ", ".join(to)
        msg.attach(MIMEText(body_html, "html"))

        try:
            smtp = aiosmtplib.SMTP(
                hostname=self._settings.smtp_host,
                port=self._settings.smtp_port,
            )
            await smtp.connect()
            await smtp.starttls()
            if self._settings.smtp_user:
                await smtp.login(self._settings.smtp_user, self._settings.smtp_password)
            await smtp.sendmail(
                self._settings.notification_from,
                to,
                msg.as_string(),
            )
            await smtp.quit()
        except Exception as exc:
            logger.error("smtp_send_failed", recipients=to, subject=subject, error=str(exc))
            raise NotificationError(f"Failed to send email: {exc}") from exc

    async def send_action_items(self, owner_email: str, items: list[ActionItem]) -> None:
        """Send an action-item digest email to a dataset owner."""
        if not self._settings.notification_enabled:
            logger.info(
                "notification_noop",
                action="send_action_items",
                owner=owner_email,
                item_count=len(items),
            )
            return

        if not items:
            logger.info("send_action_items_empty", owner=owner_email)
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

        await self.send_email(
            to=[owner_email],
            subject="DataSpoke: Action Items for Your Datasets",
            body_html=body,
        )

    async def send_sla_alert(self, recipients: list[str], alert: SLAAlert) -> None:
        """Send an SLA breach prediction alert."""
        if not self._settings.notification_enabled:
            logger.info(
                "notification_noop",
                action="send_sla_alert",
                recipients=recipients,
                dataset_urn=alert.dataset_urn,
            )
            return

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

        await self.send_email(
            to=recipients,
            subject=f"DataSpoke SLA Alert: {alert.sla_name}",
            body_html=body,
        )

    async def send_alarm(
        self,
        recipients: list[str],
        metric_id: str,
        value: float,
        threshold: float,
    ) -> None:
        """Send a metric alarm notification."""
        if not self._settings.notification_enabled:
            logger.info(
                "notification_noop",
                action="send_alarm",
                recipients=recipients,
                metric_id=metric_id,
                value=value,
                threshold=threshold,
            )
            return

        body = (
            "<h2>Metric Alarm</h2>"
            f"<p><strong>Metric:</strong> {html.escape(metric_id)}</p>"
            f"<p><strong>Current Value:</strong> {value}</p>"
            f"<p><strong>Threshold:</strong> {threshold}</p>"
        )

        await self.send_email(
            to=recipients,
            subject=f"DataSpoke Alarm: {metric_id}",
            body_html=body,
        )
