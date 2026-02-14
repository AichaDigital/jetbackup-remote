"""Tests for jetbackup_remote.models."""

import time
import unittest

from jetbackup_remote.models import (
    DestinationState, Job, JobRun, JobStatus, JobType, QueueGroupStatus,
    RunResult, Server, ServerRun,
)


class TestServer(unittest.TestCase):

    def test_defaults(self):
        s = Server(name="srv1", host="srv1.example.com")
        self.assertEqual(s.port, 51514)
        self.assertEqual(s.user, "root")
        self.assertIsNone(s.ssh_key)
        self.assertEqual(s.ssh_timeout, 30)

    def test_ssh_target(self):
        s = Server(name="srv1", host="srv1.example.com", user="backup")
        self.assertEqual(s.ssh_target(), "backup@srv1.example.com")

    def test_custom_port(self):
        s = Server(name="srv1", host="srv1.example.com", port=22)
        self.assertEqual(s.port, 22)


class TestJob(unittest.TestCase):

    def test_display_name_with_label(self):
        j = Job(job_id="abc123", server_name="srv1", label="My Backup")
        self.assertEqual(j.display_name, "My Backup")

    def test_display_name_without_label(self):
        j = Job(job_id="abc123", server_name="srv1")
        self.assertEqual(j.display_name, "abc123")

    def test_default_type(self):
        j = Job(job_id="abc123", server_name="srv1")
        self.assertEqual(j.job_type, JobType.OTHER)

    def test_priority_default(self):
        j = Job(job_id="abc123", server_name="srv1")
        self.assertEqual(j.priority, 0)


class TestJobRun(unittest.TestCase):

    def _make_job(self):
        return Job(job_id="abc", server_name="srv1", label="Test")

    def test_initial_status(self):
        jr = JobRun(job=self._make_job())
        self.assertEqual(jr.status, JobStatus.PENDING)
        self.assertIsNone(jr.started_at)
        self.assertIsNone(jr.finished_at)

    def test_start(self):
        jr = JobRun(job=self._make_job())
        jr.start()
        self.assertEqual(jr.status, JobStatus.RUNNING)
        self.assertIsNotNone(jr.started_at)

    def test_complete(self):
        jr = JobRun(job=self._make_job())
        jr.start()
        jr.complete()
        self.assertEqual(jr.status, JobStatus.COMPLETED)
        self.assertIsNotNone(jr.finished_at)

    def test_fail(self):
        jr = JobRun(job=self._make_job())
        jr.start()
        jr.fail("SSH timeout")
        self.assertEqual(jr.status, JobStatus.FAILED)
        self.assertEqual(jr.error_message, "SSH timeout")

    def test_timeout(self):
        jr = JobRun(job=self._make_job())
        jr.start()
        jr.timeout()
        self.assertEqual(jr.status, JobStatus.TIMEOUT)

    def test_skip(self):
        jr = JobRun(job=self._make_job())
        jr.skip("Server unreachable")
        self.assertEqual(jr.status, JobStatus.SKIPPED)
        self.assertEqual(jr.error_message, "Server unreachable")

    def test_duration_not_started(self):
        jr = JobRun(job=self._make_job())
        self.assertIsNone(jr.duration_seconds)
        self.assertEqual(jr.duration_human, "-")

    def test_duration_completed(self):
        jr = JobRun(job=self._make_job())
        jr.started_at = 1000.0
        jr.finished_at = 1065.0
        self.assertAlmostEqual(jr.duration_seconds, 65.0)
        self.assertEqual(jr.duration_human, "1m 5s")

    def test_duration_hours(self):
        jr = JobRun(job=self._make_job())
        jr.started_at = 1000.0
        jr.finished_at = 5600.0  # 4600 seconds = 1h 16m 40s
        self.assertEqual(jr.duration_human, "1h 16m 40s")


class TestRunResult(unittest.TestCase):

    def _make_job_run(self, status):
        j = Job(job_id="abc", server_name="srv1")
        jr = JobRun(job=j)
        jr.status = status
        return jr

    def test_empty_result(self):
        r = RunResult()
        self.assertEqual(r.total, 0)
        self.assertTrue(r.success)

    def test_counts(self):
        r = RunResult()
        r.add_job_run(self._make_job_run(JobStatus.COMPLETED))
        r.add_job_run(self._make_job_run(JobStatus.COMPLETED))
        r.add_job_run(self._make_job_run(JobStatus.FAILED))
        r.add_job_run(self._make_job_run(JobStatus.TIMEOUT))
        r.add_job_run(self._make_job_run(JobStatus.SKIPPED))

        self.assertEqual(r.total, 5)
        self.assertEqual(r.completed, 2)
        self.assertEqual(r.failed, 1)
        self.assertEqual(r.timed_out, 1)
        self.assertEqual(r.skipped, 1)
        self.assertFalse(r.success)

    def test_success_all_completed(self):
        r = RunResult()
        r.add_job_run(self._make_job_run(JobStatus.COMPLETED))
        r.add_job_run(self._make_job_run(JobStatus.COMPLETED))
        self.assertTrue(r.success)

    def test_summary_line(self):
        r = RunResult()
        r.add_job_run(self._make_job_run(JobStatus.COMPLETED))
        r.add_job_run(self._make_job_run(JobStatus.FAILED))
        summary = r.summary_line()
        self.assertIn("Total: 2", summary)
        self.assertIn("OK: 1", summary)
        self.assertIn("FAILED: 1", summary)


class TestEnums(unittest.TestCase):

    def test_job_status_values(self):
        self.assertEqual(JobStatus.PENDING.value, "pending")
        self.assertEqual(JobStatus.RUNNING.value, "running")

    def test_queue_group_status(self):
        self.assertEqual(QueueGroupStatus.RUNNING, 2)
        self.assertEqual(QueueGroupStatus.COMPLETED, 100)

    def test_job_type_values(self):
        self.assertEqual(JobType.ACCOUNTS.value, "accounts")
        self.assertEqual(JobType.DIRECTORIES.value, "directories")
        self.assertEqual(JobType.DATABASE.value, "database")


class TestQueueGroupStatusExtended(unittest.TestCase):

    def test_new_status_values(self):
        self.assertEqual(QueueGroupStatus.WARNINGS, 101)
        self.assertEqual(QueueGroupStatus.PARTIAL, 102)
        self.assertEqual(QueueGroupStatus.FAILED, 200)
        self.assertEqual(QueueGroupStatus.ABORTED, 201)

    def test_is_finished(self):
        self.assertFalse(QueueGroupStatus.RUNNING.is_finished)
        self.assertTrue(QueueGroupStatus.COMPLETED.is_finished)
        self.assertTrue(QueueGroupStatus.WARNINGS.is_finished)
        self.assertTrue(QueueGroupStatus.PARTIAL.is_finished)
        self.assertTrue(QueueGroupStatus.FAILED.is_finished)
        self.assertTrue(QueueGroupStatus.ABORTED.is_finished)

    def test_is_success(self):
        self.assertTrue(QueueGroupStatus.COMPLETED.is_success)
        self.assertTrue(QueueGroupStatus.WARNINGS.is_success)
        self.assertFalse(QueueGroupStatus.PARTIAL.is_success)
        self.assertFalse(QueueGroupStatus.FAILED.is_success)
        self.assertFalse(QueueGroupStatus.RUNNING.is_success)

    def test_is_problematic(self):
        self.assertTrue(QueueGroupStatus.PARTIAL.is_problematic)
        self.assertTrue(QueueGroupStatus.FAILED.is_problematic)
        self.assertTrue(QueueGroupStatus.ABORTED.is_problematic)
        self.assertFalse(QueueGroupStatus.COMPLETED.is_problematic)
        self.assertFalse(QueueGroupStatus.WARNINGS.is_problematic)
        self.assertFalse(QueueGroupStatus.RUNNING.is_problematic)


class TestDestinationState(unittest.TestCase):

    def test_values(self):
        self.assertEqual(DestinationState.ENABLED.value, "enabled")
        self.assertEqual(DestinationState.DISABLED.value, "disabled")
        self.assertEqual(DestinationState.UNKNOWN.value, "unknown")


class TestServerRun(unittest.TestCase):

    def test_defaults(self):
        sr = ServerRun(server_name="srv1")
        self.assertEqual(sr.server_name, "srv1")
        self.assertIsNone(sr.destination_id)
        self.assertFalse(sr.destination_activated)
        self.assertFalse(sr.destination_deactivated)
        self.assertIsNone(sr.activation_error)
        self.assertIsNone(sr.deactivation_error)
        self.assertEqual(sr.job_runs, [])
        self.assertEqual(sr.orphan_queue_groups_killed, 0)

    def test_has_lifecycle_error(self):
        sr = ServerRun(server_name="srv1")
        self.assertFalse(sr.has_lifecycle_error)
        sr.activation_error = "failed to activate"
        self.assertTrue(sr.has_lifecycle_error)

    def test_has_deactivation_error(self):
        sr = ServerRun(server_name="srv1")
        self.assertFalse(sr.has_deactivation_error)
        sr.deactivation_error = "CRITICAL: failed"
        self.assertTrue(sr.has_deactivation_error)

    def test_with_destination(self):
        sr = ServerRun(server_name="srv1", destination_id="dest1")
        self.assertEqual(sr.destination_id, "dest1")


class TestJobRunVerification(unittest.TestCase):

    def _make_job(self):
        return Job(job_id="abc", server_name="srv1", label="Test")

    def test_verification_fields_default_none(self):
        jr = JobRun(job=self._make_job())
        self.assertIsNone(jr.queue_group_id)
        self.assertIsNone(jr.queue_group_status)
        self.assertIsNone(jr.log_id)
        self.assertIsNone(jr.log_contents)

    def test_is_verified_problematic_without_status(self):
        jr = JobRun(job=self._make_job())
        self.assertFalse(jr.is_verified_problematic)

    def test_is_verified_problematic_with_partial(self):
        jr = JobRun(job=self._make_job())
        jr.queue_group_status = QueueGroupStatus.PARTIAL
        self.assertTrue(jr.is_verified_problematic)

    def test_is_verified_problematic_with_completed(self):
        jr = JobRun(job=self._make_job())
        jr.queue_group_status = QueueGroupStatus.COMPLETED
        self.assertFalse(jr.is_verified_problematic)


class TestRunResultExtended(unittest.TestCase):

    def test_partial_jobs_count(self):
        r = RunResult()
        j = Job(job_id="abc", server_name="srv1")
        jr = JobRun(job=j)
        jr.status = JobStatus.COMPLETED
        jr.queue_group_status = QueueGroupStatus.PARTIAL
        r.add_job_run(jr)

        j2 = Job(job_id="def", server_name="srv1")
        jr2 = JobRun(job=j2)
        jr2.status = JobStatus.COMPLETED
        r.add_job_run(jr2)

        self.assertEqual(r.partial_jobs, 1)

    def test_has_deactivation_errors(self):
        r = RunResult()
        sr = ServerRun(server_name="srv1")
        r.add_server_run(sr)
        self.assertFalse(r.has_deactivation_errors)

        sr2 = ServerRun(server_name="srv2", deactivation_error="CRITICAL")
        r.add_server_run(sr2)
        self.assertTrue(r.has_deactivation_errors)

    def test_summary_line_with_partial(self):
        r = RunResult()
        j = Job(job_id="abc", server_name="srv1")
        jr = JobRun(job=j)
        jr.status = JobStatus.COMPLETED
        jr.queue_group_status = QueueGroupStatus.PARTIAL
        r.add_job_run(jr)
        self.assertIn("PARTIAL: 1", r.summary_line())

    def test_server_destination_id(self):
        s = Server(name="srv1", host="h", destination_id="dest1")
        self.assertEqual(s.destination_id, "dest1")


if __name__ == "__main__":
    unittest.main()
