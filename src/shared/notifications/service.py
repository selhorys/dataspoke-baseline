"""Notification service — email delivery with master toggle."""

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)


class NotificationService:
    """Sends notifications via configured channels.

    When ``enabled`` is False (the default for development), all methods are
    no-ops that only log what *would* have been sent.
    """

    def __init__(
        self,
        *,
        enabled: bool = False,
        smtp_host: str = "",
        smtp_port: int = 587,
        smtp_user: str = "",
        smtp_password: str = "",
        sender: str = "dataspoke@example.com",
    ) -> None:
        self._enabled = enabled
        self._smtp_host = smtp_host
        self._smtp_port = smtp_port
        self._smtp_user = smtp_user
        self._smtp_password = smtp_password
        self._sender = sender

    async def send_email(self, to: list[str], subject: str, body_html: str) -> None:
        """Send an email notification via SMTP."""
        if not self._enabled:
            logger.info("Notification disabled — skipping email to %s: %s", to, subject)
            return

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = self._sender
        msg["To"] = ", ".join(to)
        msg.attach(MIMEText(body_html, "html"))

        with smtplib.SMTP(self._smtp_host, self._smtp_port) as server:
            if self._smtp_user:
                server.starttls()
                server.login(self._smtp_user, self._smtp_password)
            server.sendmail(self._sender, to, msg.as_string())

        logger.info("Email sent to %s: %s", to, subject)

    async def send_action_items(self, owner_email: str, items: list[dict]) -> None:
        """Send a formatted email with prioritized action items."""
        if not items:
            return

        rows = "".join(
            f"<tr><td>{it.get('issue_type', '')}</td>"
            f"<td>{it.get('priority', '')}</td>"
            f"<td>{it.get('description', '')}</td></tr>"
            for it in items
        )
        body = (
            "<h2>DataSpoke Action Items</h2>"
            f"<table><tr><th>Issue</th><th>Priority</th><th>Description</th></tr>{rows}</table>"
        )
        await self.send_email([owner_email], "DataSpoke: Action Items", body)

    async def send_sla_alert(
        self,
        recipients: list[str],
        alert: dict,
    ) -> None:
        """Send a pre-breach SLA warning."""
        body = (
            "<h2>SLA Alert</h2>"
            f"<p>Dataset: {alert.get('dataset_urn', 'unknown')}</p>"
            f"<p>Predicted breach: {alert.get('predicted_breach_time', 'N/A')}</p>"
            f"<p>{alert.get('recommended_actions', '')}</p>"
        )
        await self.send_email(recipients, "DataSpoke: SLA Alert", body)

    async def send_alarm(
        self,
        recipients: list[str],
        metric_id: str,
        value: float,
        threshold: float,
    ) -> None:
        """Send a metric threshold breach alarm."""
        body = (
            f"<h2>Metric Alarm: {metric_id}</h2>"
            f"<p>Current value: {value}</p>"
            f"<p>Threshold: {threshold}</p>"
        )
        await self.send_email(recipients, f"DataSpoke: Metric Alarm — {metric_id}", body)
