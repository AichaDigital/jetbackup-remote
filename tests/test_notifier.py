"""Tests for jetbackup_remote.notifier."""

import unittest
from unittest.mock import patch, MagicMock

from jetbackup_remote.config import NotificationConfig
from jetbackup_remote.models import Job, JobRun, JobStatus, RunResult
from jetbackup_remote.notifier import (
    build_summary_email, send_notification, _format_duration,
)


class TestFormatDuration(unittest.TestCase):

    def test_none(self):
        self.assertEqual(_format_duration(None), "-")

    def test_seconds(self):
        self.assertEqual(_format_duration(45), "45s")

    def test_minutes(self):
        self.assertEqual(_format_duration(125), "2m 5s")

    def test_hours(self):
        self.assertEqual(_format_duration(7265), "2h 1m 5s")


class TestBuildSummaryEmail(unittest.TestCase):

    def _make_result(self, statuses):
        result = RunResult()
        result.started_at = 1707800000.0
        result.finished_at = 1707803600.0
        for i, status in enumerate(statuses):
            j = Job(job_id=f"job{i}", server_name=f"srv{i}", label=f"Backup{i}")
            jr = JobRun(job=j)
            jr.status = status
            jr.started_at = 1707800000.0
            jr.finished_at = 1707803600.0
            if status == JobStatus.FAILED:
                jr.error_message = "API error"
            result.add_job_run(jr)
        return result

    def test_success_subject(self):
        result = self._make_result([JobStatus.COMPLETED, JobStatus.COMPLETED])
        subject, body = build_summary_email(result)
        self.assertIn("OK", subject)
        self.assertIn("jetbackup-remote", subject)

    def test_failure_subject(self):
        result = self._make_result([JobStatus.COMPLETED, JobStatus.FAILED])
        subject, body = build_summary_email(result)
        self.assertIn("FAILED", subject)

    def test_timeout_subject(self):
        result = self._make_result([JobStatus.TIMEOUT])
        subject, body = build_summary_email(result)
        self.assertIn("TIMEOUT", subject)

    def test_body_contains_jobs(self):
        result = self._make_result([JobStatus.COMPLETED, JobStatus.FAILED])
        subject, body = build_summary_email(result)
        self.assertIn("Backup0", body)
        self.assertIn("Backup1", body)
        self.assertIn("API error", body)

    def test_body_contains_attention(self):
        result = self._make_result([JobStatus.FAILED])
        subject, body = build_summary_email(result)
        self.assertIn("ATTENTION REQUIRED", body)

    def test_body_dry_run(self):
        result = self._make_result([JobStatus.COMPLETED])
        result.dry_run = True
        subject, body = build_summary_email(result)
        self.assertIn("Yes", body)


class TestSendNotification(unittest.TestCase):

    def test_disabled_skips(self):
        config = NotificationConfig(enabled=False)
        result = RunResult()
        self.assertTrue(send_notification(config, result))

    def test_no_recipients_fails(self):
        config = NotificationConfig(enabled=True, to_addresses=[])
        result = RunResult()
        self.assertFalse(send_notification(config, result))

    def test_policy_on_complete_success(self):
        config = NotificationConfig(
            enabled=True,
            to_addresses=["a@b.com"],
            on_complete=True,
            on_failure=False,
            on_timeout=False,
        )
        result = RunResult()
        result.started_at = 1.0
        result.finished_at = 2.0
        j = Job(job_id="a", server_name="s")
        jr = JobRun(job=j)
        jr.status = JobStatus.COMPLETED
        result.add_job_run(jr)

        with patch("jetbackup_remote.notifier.smtplib.SMTP") as mock_smtp:
            instance = MagicMock()
            mock_smtp.return_value = instance
            self.assertTrue(send_notification(config, result))
            instance.sendmail.assert_called_once()

    def test_policy_on_failure_only(self):
        config = NotificationConfig(
            enabled=True,
            to_addresses=["a@b.com"],
            on_complete=False,
            on_failure=True,
            on_timeout=False,
        )
        # All success - should NOT send
        result = RunResult()
        j = Job(job_id="a", server_name="s")
        jr = JobRun(job=j)
        jr.status = JobStatus.COMPLETED
        result.add_job_run(jr)

        self.assertTrue(send_notification(config, result))
        # No SMTP call expected since policy doesn't match

    def test_smtp_failure_returns_false(self):
        config = NotificationConfig(
            enabled=True,
            to_addresses=["a@b.com"],
            on_failure=True,
        )
        result = RunResult()
        result.started_at = 1.0
        result.finished_at = 2.0
        j = Job(job_id="a", server_name="s")
        jr = JobRun(job=j)
        jr.status = JobStatus.FAILED
        jr.error_message = "test"
        result.add_job_run(jr)

        with patch("jetbackup_remote.notifier.smtplib.SMTP") as mock_smtp:
            mock_smtp.side_effect = OSError("Connection refused")
            self.assertFalse(send_notification(config, result))

    def test_smtp_tls(self):
        config = NotificationConfig(
            enabled=True,
            to_addresses=["a@b.com"],
            smtp_tls=True,
            smtp_user="user",
            smtp_password="pass",
            on_failure=True,
        )
        result = RunResult()
        result.started_at = 1.0
        result.finished_at = 2.0
        j = Job(job_id="a", server_name="s")
        jr = JobRun(job=j)
        jr.status = JobStatus.FAILED
        jr.error_message = "test"
        result.add_job_run(jr)

        with patch("jetbackup_remote.notifier.smtplib.SMTP") as mock_smtp:
            instance = MagicMock()
            mock_smtp.return_value = instance
            self.assertTrue(send_notification(config, result))
            instance.starttls.assert_called_once()
            instance.login.assert_called_once_with("user", "pass")


if __name__ == "__main__":
    unittest.main()
