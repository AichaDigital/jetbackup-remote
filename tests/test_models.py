"""Tests for jetbackup_remote.models."""

import time
import unittest

from jetbackup_remote.models import (
    Job, JobRun, JobStatus, JobType, QueueGroupStatus,
    RunResult, Server,
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


if __name__ == "__main__":
    unittest.main()
