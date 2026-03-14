"""Email notification for jetbackup-remote."""

import logging
import os
import smtplib
import socket
from email.mime.text import MIMEText
from datetime import datetime, timezone
from typing import Optional

from .config import NotificationConfig
from .models import JobStatus, QueueGroupStatus, RunResult

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


def _status_tag(result: RunResult) -> str:
    """Determine the status tag for the email subject."""
    if result.has_deactivation_errors:
        return "CRITICAL"
    if result.failed > 0:
        return "FAILED"
    if result.timed_out > 0:
        return "TIMEOUT"
    if result.partial_jobs > 0:
        return "PARTIAL"
    if result.success:
        return "OK"
    return "PARTIAL"


def build_summary_email(result: RunResult) -> tuple:
    """Build email subject and body from a RunResult.

    Returns:
        Tuple of (subject, body).
    """
    tag = _status_tag(result)

    subject = f"[jetbackup-remote] {tag} - {result.summary_line()}"

    lines = [
        f"jetbackup-remote run summary",
        f"{'=' * 40}",
        f"",
        f"Status:    {tag}",
        f"Started:   {datetime.fromtimestamp(result.started_at, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC') if result.started_at else '-'}",
        f"Finished:  {datetime.fromtimestamp(result.finished_at, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC') if result.finished_at else '-'}",
        f"Duration:  {_format_duration(result.duration_seconds)}",
        f"Dry run:   {'Yes' if result.dry_run else 'No'}",
        f"",
        f"Results:   {result.summary_line()}",
        f"",
    ]

    if result.jobs:
        lines.append(f"{'Job':<30} {'Server':<15} {'Status':<10} {'Duration':<12} {'QG Status'}")
        lines.append(f"{'-'*30} {'-'*15} {'-'*10} {'-'*12} {'-'*12}")

        for jr in result.jobs:
            name = jr.job.display_name[:29]
            server = jr.job.server_name[:14]
            status = jr.status.value
            duration = jr.duration_human
            qg_status = jr.queue_group_status.name if jr.queue_group_status else "-"
            lines.append(f"{name:<30} {server:<15} {status:<10} {duration:<12} {qg_status}")

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

    # JetBackup verification details for partial/failed jobs
    problematic = [
        jr for jr in result.jobs
        if jr.queue_group_status and jr.queue_group_status.is_problematic
    ]
    if problematic:
        lines.append("JETBACKUP VERIFICATION:")
        lines.append("-" * 40)
        for jr in problematic:
            lines.append(
                f"  {jr.job.display_name} on {jr.job.server_name}: "
                f"queue_group={jr.queue_group_id} status={jr.queue_group_status.name}"
            )
            if jr.log_contents:
                lines.append(f"    Log contents:")
                for log_line in jr.log_contents.splitlines()[:20]:
                    lines.append(f"      {log_line}")
                if len(jr.log_contents.splitlines()) > 20:
                    lines.append(f"      ... ({len(jr.log_contents.splitlines()) - 20} more lines)")
        lines.append("")

    # Destination lifecycle errors (CRITICAL)
    deactivation_errors = [
        sr for sr in result.server_runs
        if sr.has_deactivation_error
    ]
    if deactivation_errors:
        lines.append("DESTINATION LIFECYCLE ISSUES (CRITICAL):")
        lines.append("=" * 40)
        for sr in deactivation_errors:
            lines.append(f"  Server: {sr.server_name}")
            lines.append(f"  Destination: {sr.destination_id}")
            if sr.deactivation_error:
                lines.append(f"  ERROR: {sr.deactivation_error}")
            lines.append(f"  ACTION REQUIRED: Manually disable destination!")
            lines.append("")

    lines.append(f"--")
    lines.append(f"jetbackup-remote on {socket.gethostname()}")

    body = "\n".join(lines)
    return subject, body


def _send_email(config: NotificationConfig, subject: str, body: str) -> bool:
    """Send a single email. Never raises — returns False on failure."""
    if not config.enabled or not config.to_addresses:
        return True

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
        logger.info("Email sent: %s", subject)
        return True
    except (smtplib.SMTPException, OSError) as e:
        logger.warning("Failed to send email '%s': %s", subject, e)
        return False


def build_timeout_email(
    server_name: str,
    job_label: str,
    job_type: str,
    duration_seconds: float,
    cycle_progress: str,
) -> tuple:
    """Build timeout alert email subject and body."""
    subject = f"[jetbackup-remote] TIMEOUT: {job_label} on {server_name}"
    lines = [
        f"Job: {job_label} ({job_type})",
        f"Server: {server_name}",
        f"Duration: {_format_duration(duration_seconds)}",
        f"Action: Job aborted via stopQueueGroup",
        f"Status: Moving to next job",
        f"",
        f"Cycle progress: {cycle_progress}",
        f"",
        f"--",
        f"jetbackup-remote on {socket.gethostname()}",
    ]
    return subject, "\n".join(lines)


def build_lifecycle_email(event: str, hostname: str) -> tuple:
    """Build daemon lifecycle email (started/stopping)."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    subject = f"[jetbackup-remote] Daemon {event} on {hostname}"
    lines = [
        f"jetbackup-remote daemon {event}",
        f"Host: {hostname}",
        f"Time: {now}",
        f"PID: {os.getpid()}",
        f"",
        f"--",
        f"jetbackup-remote on {hostname}",
    ]
    return subject, "\n".join(lines)


def send_notification(
    config: NotificationConfig,
    result: RunResult,
) -> bool:
    """Send email notification about a run result.

    Respects config flags: on_failure, on_timeout, on_complete, on_partial.
    ALWAYS sends if there are deactivation errors (CRITICAL).

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
    if config.on_partial and result.partial_jobs > 0:
        should_send = True

    # ALWAYS send for deactivation errors (CRITICAL)
    if result.has_deactivation_errors:
        should_send = True
        logger.warning("Forcing notification due to destination deactivation error")

    if not should_send:
        logger.debug("Notification not triggered by current policy")
        return True

    subject, body = build_summary_email(result)
    return _send_email(config, subject, body)


def send_timeout_alert(config, server_name, job_label, job_type, duration_seconds, cycle_progress):
    """Send immediate timeout alert email. Never raises."""
    if not getattr(config, "on_timeout", True):
        return True
    subject, body = build_timeout_email(server_name, job_label, job_type, duration_seconds, cycle_progress)
    return _send_email(config, subject, body)


def send_daemon_lifecycle(config, event, hostname=None):
    """Send daemon lifecycle email (started/stopping). Never raises."""
    if not getattr(config, "on_daemon_lifecycle", True):
        return True
    hostname = hostname or socket.gethostname()
    subject, body = build_lifecycle_email(event, hostname)
    return _send_email(config, subject, body)


def send_cycle_summary(config, result):
    """Send cycle summary email. Never raises."""
    try:
        return send_notification(config, result)
    except Exception as e:
        logger.warning("Failed to send cycle summary: %s", e)
        return False
