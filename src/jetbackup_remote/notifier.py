"""Email notification for jetbackup-remote."""

import logging
import smtplib
import socket
from email.mime.text import MIMEText
from datetime import datetime, timezone
from typing import Optional

from .config import NotificationConfig
from .models import JobStatus, RunResult

logger = logging.getLogger("jetbackup_remote.notifier")


class NotificationError(Exception):
    """Raised when notification delivery fails."""
    pass


def _format_duration(seconds: Optional[float]) -> str:
    """Format seconds into human-readable duration."""
    if seconds is None:
        return "-"
    hours, remainder = divmod(int(seconds), 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def build_summary_email(result: RunResult) -> tuple:
    """Build email subject and body from a RunResult.

    Returns:
        Tuple of (subject, body).
    """
    if result.success:
        status_tag = "OK"
    elif result.failed > 0:
        status_tag = "FAILED"
    elif result.timed_out > 0:
        status_tag = "TIMEOUT"
    else:
        status_tag = "PARTIAL"

    subject = f"[jetbackup-remote] {status_tag} - {result.summary_line()}"

    lines = [
        f"jetbackup-remote run summary",
        f"{'=' * 40}",
        f"",
        f"Status:    {status_tag}",
        f"Started:   {datetime.fromtimestamp(result.started_at, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC') if result.started_at else '-'}",
        f"Finished:  {datetime.fromtimestamp(result.finished_at, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC') if result.finished_at else '-'}",
        f"Duration:  {_format_duration(result.duration_seconds)}",
        f"Dry run:   {'Yes' if result.dry_run else 'No'}",
        f"",
        f"Results:   {result.summary_line()}",
        f"",
    ]

    if result.jobs:
        lines.append(f"{'Job':<30} {'Server':<15} {'Status':<10} {'Duration':<12}")
        lines.append(f"{'-'*30} {'-'*15} {'-'*10} {'-'*12}")

        for jr in result.jobs:
            name = jr.job.display_name[:29]
            server = jr.job.server_name[:14]
            status = jr.status.value
            duration = jr.duration_human
            lines.append(f"{name:<30} {server:<15} {status:<10} {duration:<12}")

            if jr.error_message:
                lines.append(f"  Error: {jr.error_message}")

        lines.append("")

    # Failures detail
    failures = [jr for jr in result.jobs if jr.status in (JobStatus.FAILED, JobStatus.TIMEOUT)]
    if failures:
        lines.append("ATTENTION REQUIRED:")
        lines.append("-" * 40)
        for jr in failures:
            lines.append(f"  {jr.job.display_name} on {jr.job.server_name}: {jr.status.value}")
            if jr.error_message:
                lines.append(f"    {jr.error_message}")
        lines.append("")

    lines.append(f"--")
    lines.append(f"jetbackup-remote on {socket.gethostname()}")

    body = "\n".join(lines)
    return subject, body


def send_notification(
    config: NotificationConfig,
    result: RunResult,
) -> bool:
    """Send email notification about a run result.

    Respects config flags: on_failure, on_timeout, on_complete.

    Args:
        config: Notification configuration.
        result: The run result to report.

    Returns:
        True if email was sent (or skipped by policy), False on error.
    """
    if not config.enabled:
        logger.debug("Notifications disabled, skipping")
        return True

    if not config.to_addresses:
        logger.warning("No notification recipients configured")
        return False

    # Check if we should send based on policy
    should_send = False
    if config.on_complete and result.success:
        should_send = True
    if config.on_failure and result.failed > 0:
        should_send = True
    if config.on_timeout and result.timed_out > 0:
        should_send = True

    if not should_send:
        logger.debug("Notification not triggered by current policy")
        return True

    subject, body = build_summary_email(result)

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = config.from_address
    msg["To"] = ", ".join(config.to_addresses)

    try:
        if config.smtp_tls:
            smtp = smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=30)
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
        else:
            smtp = smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=30)

        if config.smtp_user and config.smtp_password:
            smtp.login(config.smtp_user, config.smtp_password)

        smtp.sendmail(config.from_address, config.to_addresses, msg.as_string())
        smtp.quit()

        logger.info("Notification sent to %s", ", ".join(config.to_addresses))
        return True

    except (smtplib.SMTPException, OSError) as e:
        logger.error("Failed to send notification: %s", e)
        return False
