"""Tests for jetbackup_remote.notifier."""

import unittest
from unittest.mock import patch, MagicMock

from jetbackup_remote.config import NotificationConfig
from jetbackup_remote.models import Job, JobRun, JobStatus, QueueGroupStatus, RunResult, ServerRun
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


class TestPartialNotification(unittest.TestCase):

    def _make_partial_result(self):
        result = RunResult()
        result.started_at = 1707800000.0
        result.finished_at = 1707803600.0
        j = Job(job_id="job1", server_name="srv1", label="Accounts")
        jr = JobRun(job=j)
        jr.status = JobStatus.COMPLETED
        jr.started_at = 1707800000.0
        jr.finished_at = 1707803600.0
        jr.queue_group_id = "qg_partial"
        jr.queue_group_status = QueueGroupStatus.PARTIAL
        jr.log_contents = "Partial backup: 3 of 5 accounts completed\nAccount user3: disk quota exceeded"
        result.add_job_run(jr)
        return result

    def test_partial_subject_tag(self):
        result = self._make_partial_result()
        subject, body = build_summary_email(result)
        self.assertIn("PARTIAL", subject)

    def test_partial_body_verification_section(self):
        result = self._make_partial_result()
        subject, body = build_summary_email(result)
        self.assertIn("JETBACKUP VERIFICATION", body)
        self.assertIn("qg_partial", body)
        self.assertIn("PARTIAL", body)

    def test_partial_body_log_contents(self):
        result = self._make_partial_result()
        subject, body = build_summary_email(result)
        self.assertIn("Partial backup", body)
        self.assertIn("disk quota exceeded", body)

    def test_on_partial_policy_triggers(self):
        config = NotificationConfig(
            enabled=True,
            to_addresses=["a@b.com"],
            on_complete=False,
            on_failure=False,
            on_timeout=False,
            on_partial=True,
        )
        result = self._make_partial_result()

        with patch("jetbackup_remote.notifier.smtplib.SMTP") as mock_smtp:
            instance = MagicMock()
            mock_smtp.return_value = instance
            self.assertTrue(send_notification(config, result))
            instance.sendmail.assert_called_once()

    def test_on_partial_disabled_skips(self):
        config = NotificationConfig(
            enabled=True,
            to_addresses=["a@b.com"],
            on_complete=False,
            on_failure=False,
            on_timeout=False,
            on_partial=False,
        )
        result = self._make_partial_result()
        # Should not trigger (on_partial=False and success=True)
        sent = send_notification(config, result)
        self.assertTrue(sent)  # True means "skipped by policy" (not an error)


class TestDeactivationErrorNotification(unittest.TestCase):

    def _make_deactivation_error_result(self):
        result = RunResult()
        result.started_at = 1707800000.0
        result.finished_at = 1707803600.0

        j = Job(job_id="job1", server_name="srv1", label="Backup1")
        jr = JobRun(job=j)
        jr.status = JobStatus.COMPLETED
        jr.started_at = 1707800000.0
        jr.finished_at = 1707803600.0
        result.add_job_run(jr)

        sr = ServerRun(
            server_name="srv1",
            destination_id="dest1",
            destination_activated=True,
            deactivation_error="CRITICAL: Failed to disable destination",
        )
        result.add_server_run(sr)
        return result

    def test_critical_subject(self):
        result = self._make_deactivation_error_result()
        subject, body = build_summary_email(result)
        self.assertIn("CRITICAL", subject)

    def test_critical_body_section(self):
        result = self._make_deactivation_error_result()
        subject, body = build_summary_email(result)
        self.assertIn("DESTINATION LIFECYCLE ISSUES", body)
        self.assertIn("ACTION REQUIRED", body)
        self.assertIn("Manually disable destination", body)

    def test_critical_always_sends(self):
        """Deactivation error ALWAYS triggers notification regardless of policy."""
        config = NotificationConfig(
            enabled=True,
            to_addresses=["a@b.com"],
            on_complete=False,
            on_failure=False,
            on_timeout=False,
            on_partial=False,
        )
        result = self._make_deactivation_error_result()

        with patch("jetbackup_remote.notifier.smtplib.SMTP") as mock_smtp:
            instance = MagicMock()
            mock_smtp.return_value = instance
            self.assertTrue(send_notification(config, result))
            instance.sendmail.assert_called_once()


class TestQGStatusInEmail(unittest.TestCase):

    def test_qg_status_column(self):
        result = RunResult()
        result.started_at = 1707800000.0
        result.finished_at = 1707803600.0
        j = Job(job_id="job1", server_name="srv1", label="Accounts")
        jr = JobRun(job=j)
        jr.status = JobStatus.COMPLETED
        jr.started_at = 1707800000.0
        jr.finished_at = 1707803600.0
        jr.queue_group_status = QueueGroupStatus.COMPLETED
        result.add_job_run(jr)
        subject, body = build_summary_email(result)
        self.assertIn("QG Status", body)
        self.assertIn("COMPLETED", body)


if __name__ == "__main__":
    unittest.main()
