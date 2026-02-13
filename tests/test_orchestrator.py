"""Tests for jetbackup_remote.orchestrator."""

import json
import os
import tempfile
import unittest
from unittest.mock import patch, MagicMock, call

from jetbackup_remote.config import Config, OrchestratorConfig, NotificationConfig, load_config
from jetbackup_remote.models import Job, JobRun, JobStatus, JobType, Server, RunResult
from jetbackup_remote.ssh import SSHError, SSHResult
from jetbackup_remote.jetbackup_api import JetBackupAPIError
from jetbackup_remote.orchestrator import Orchestrator, LockError


def _make_config(jobs=None, servers=None):
    """Create a test Config."""
    if servers is None:
        servers = {
            "srv1": Server(name="srv1", host="srv1.example.com"),
            "srv2": Server(name="srv2", host="srv2.example.com"),
        }
    if jobs is None:
        jobs = [
            Job(job_id="job1", server_name="srv1", label="Backup1"),
            Job(job_id="job2", server_name="srv1", label="Backup2"),
            Job(job_id="job3", server_name="srv2", label="Backup3"),
        ]

    fd, lock_path = tempfile.mkstemp()
    os.close(fd)
    os.unlink(lock_path)

    return Config(
        servers=servers,
        jobs=jobs,
        orchestrator=OrchestratorConfig(
            poll_interval=0.01,  # Fast for testing
            job_timeout=1,  # Short for testing
            startup_timeout=0.5,  # Short for testing
            lock_file=lock_path,
        ),
        notification=NotificationConfig(),
    )


class TestOrchestratorLock(unittest.TestCase):

    def test_acquire_and_release(self):
        config = _make_config()
        orch = Orchestrator(config)
        orch.acquire_lock()
        self.assertIsNotNone(orch._lock_fd)
        orch.release_lock()
        self.assertIsNone(orch._lock_fd)

    def test_double_lock_fails(self):
        config = _make_config()
        orch1 = Orchestrator(config)
        orch2 = Orchestrator(config)

        orch1.acquire_lock()
        try:
            with self.assertRaises(LockError):
                orch2.acquire_lock()
        finally:
            orch1.release_lock()

    def test_release_without_acquire(self):
        config = _make_config()
        orch = Orchestrator(config)
        # Should not raise
        orch.release_lock()


class TestBuildQueue(unittest.TestCase):

    def test_full_queue(self):
        config = _make_config()
        orch = Orchestrator(config)
        queue = orch._build_queue()
        self.assertEqual(len(queue), 3)

    def test_server_filter(self):
        config = _make_config()
        orch = Orchestrator(config)
        queue = orch._build_queue(server_filter="srv1")
        self.assertEqual(len(queue), 2)
        self.assertTrue(all(j.server_name == "srv1" for j in queue))

    def test_job_filter(self):
        config = _make_config()
        orch = Orchestrator(config)
        queue = orch._build_queue(job_filter="job2")
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0].job_id, "job2")

    def test_combined_filter(self):
        config = _make_config()
        orch = Orchestrator(config)
        queue = orch._build_queue(server_filter="srv1", job_filter="job1")
        self.assertEqual(len(queue), 1)

    def test_empty_filter(self):
        config = _make_config()
        orch = Orchestrator(config)
        queue = orch._build_queue(server_filter="nonexistent")
        self.assertEqual(len(queue), 0)


class TestDryRun(unittest.TestCase):

    def test_dry_run_completes_all(self):
        config = _make_config()
        orch = Orchestrator(config)
        result = orch.run(dry_run=True)
        self.assertEqual(result.total, 3)
        self.assertEqual(result.completed, 3)
        self.assertTrue(result.success)
        self.assertTrue(result.dry_run)

    def test_dry_run_with_filter(self):
        config = _make_config()
        orch = Orchestrator(config)
        result = orch.run(dry_run=True, server_filter="srv2")
        self.assertEqual(result.total, 1)
        self.assertEqual(result.completed, 1)

    def test_dry_run_empty_queue(self):
        config = _make_config()
        orch = Orchestrator(config)
        result = orch.run(dry_run=True, server_filter="nonexistent")
        self.assertEqual(result.total, 0)


class TestExecuteJob(unittest.TestCase):

    @patch("jetbackup_remote.orchestrator.is_job_running")
    @patch("jetbackup_remote.orchestrator.run_job")
    @patch("jetbackup_remote.orchestrator.test_connection")
    def test_successful_execution(self, mock_test, mock_run, mock_is_running):
        """Trigger → startup wait sees running=True → poll sees running=False."""
        mock_test.return_value = {"ssh_ok": True, "jetbackup_ok": True, "error": None}
        mock_run.return_value = True
        # 1: not running (check) → trigger
        # 2: running (startup wait) → started
        # 3: not running (completion poll) → done
        mock_is_running.side_effect = [False, True, False]

        config = _make_config(jobs=[
            Job(job_id="job1", server_name="srv1", label="Backup1"),
        ])
        orch = Orchestrator(config)
        result = orch.run()

        self.assertEqual(result.total, 1)
        self.assertEqual(result.completed, 1)
        mock_run.assert_called_once()

    @patch("jetbackup_remote.orchestrator.is_job_running")
    @patch("jetbackup_remote.orchestrator.run_job")
    @patch("jetbackup_remote.orchestrator.test_connection")
    def test_job_already_running(self, mock_test, mock_run, mock_is_running):
        """Already running → skip trigger and startup, go to completion poll."""
        mock_test.return_value = {"ssh_ok": True, "jetbackup_ok": True, "error": None}
        # 1: already running (check) → skip trigger
        # 2: not running (completion poll) → done
        mock_is_running.side_effect = [True, False]

        config = _make_config(jobs=[
            Job(job_id="job1", server_name="srv1", label="Backup1"),
        ])
        orch = Orchestrator(config)
        result = orch.run()

        self.assertEqual(result.completed, 1)
        mock_run.assert_not_called()  # Should not trigger if already running

    @patch("jetbackup_remote.orchestrator.test_connection")
    def test_ssh_unreachable_skips(self, mock_test):
        mock_test.return_value = {
            "ssh_ok": False, "jetbackup_ok": False,
            "error": "Connection refused",
        }

        config = _make_config(jobs=[
            Job(job_id="job1", server_name="srv1", label="Backup1"),
        ])
        orch = Orchestrator(config)
        result = orch.run()

        self.assertEqual(result.total, 1)
        self.assertEqual(result.skipped, 1)

    @patch("jetbackup_remote.orchestrator.is_job_running")
    @patch("jetbackup_remote.orchestrator.run_job")
    @patch("jetbackup_remote.orchestrator.test_connection")
    def test_trigger_failure(self, mock_test, mock_run, mock_is_running):
        mock_test.return_value = {"ssh_ok": True, "jetbackup_ok": True, "error": None}
        mock_is_running.return_value = False
        mock_run.side_effect = JetBackupAPIError("API error")

        config = _make_config(jobs=[
            Job(job_id="job1", server_name="srv1", label="Backup1"),
        ])
        orch = Orchestrator(config)
        result = orch.run()

        self.assertEqual(result.total, 1)
        self.assertEqual(result.failed, 1)

    @patch("jetbackup_remote.orchestrator.is_job_running")
    @patch("jetbackup_remote.orchestrator.run_job")
    @patch("jetbackup_remote.orchestrator.test_connection")
    def test_timeout_does_not_abort(self, mock_test, mock_run, mock_is_running):
        """Job starts but never finishes → timeout without aborting."""
        mock_test.return_value = {"ssh_ok": True, "jetbackup_ok": True, "error": None}
        mock_run.return_value = True
        # 1: not running (check) → trigger
        # 2: running (startup) → started
        # 3+: always running → eventually times out
        mock_is_running.side_effect = [False, True] + [True] * 200

        config = _make_config(jobs=[
            Job(job_id="job1", server_name="srv1", label="Backup1"),
        ])
        config.orchestrator.job_timeout = 0.05  # 50ms timeout
        config.orchestrator.poll_interval = 0.01

        orch = Orchestrator(config)
        result = orch.run()

        self.assertEqual(result.total, 1)
        self.assertEqual(result.timed_out, 1)
        # Job was NOT stopped/aborted
        self.assertFalse(result.success)

    @patch("jetbackup_remote.orchestrator.is_job_running")
    @patch("jetbackup_remote.orchestrator.run_job")
    @patch("jetbackup_remote.orchestrator.test_connection")
    def test_startup_timeout_fails(self, mock_test, mock_run, mock_is_running):
        """Job triggered but never starts within startup_timeout → fail."""
        mock_test.return_value = {"ssh_ok": True, "jetbackup_ok": True, "error": None}
        mock_run.return_value = True
        # 1: not running (check) → trigger
        # 2+: never starts (always False) → startup timeout
        mock_is_running.return_value = False

        config = _make_config(jobs=[
            Job(job_id="job1", server_name="srv1", label="Backup1"),
        ])
        config.orchestrator.startup_timeout = 0.05  # 50ms
        config.orchestrator.poll_interval = 0.01

        orch = Orchestrator(config)
        result = orch.run()

        self.assertEqual(result.total, 1)
        self.assertEqual(result.failed, 1)
        mock_run.assert_called_once()

    @patch("jetbackup_remote.orchestrator.is_job_running")
    @patch("jetbackup_remote.orchestrator.run_job")
    @patch("jetbackup_remote.orchestrator.test_connection")
    def test_multiple_jobs_sequential(self, mock_test, mock_run, mock_is_running):
        """All 3 jobs run sequentially: check→trigger→startup→complete each."""
        mock_test.return_value = {"ssh_ok": True, "jetbackup_ok": True, "error": None}
        mock_run.return_value = True
        # Each job: False (check) → True (startup) → False (complete)
        mock_is_running.side_effect = [
            False, True, False,  # job1
            False, True, False,  # job2
            False, True, False,  # job3
        ]

        config = _make_config()
        orch = Orchestrator(config)
        result = orch.run()

        self.assertEqual(result.total, 3)
        self.assertEqual(result.completed, 3)
        self.assertEqual(mock_run.call_count, 3)


class TestShutdown(unittest.TestCase):

    @patch("jetbackup_remote.orchestrator.is_job_running")
    @patch("jetbackup_remote.orchestrator.run_job")
    @patch("jetbackup_remote.orchestrator.test_connection")
    def test_shutdown_skips_remaining(self, mock_test, mock_run, mock_is_running):
        mock_test.return_value = {"ssh_ok": True, "jetbackup_ok": True, "error": None}
        mock_run.return_value = True
        # First job: False (check) → True (startup) → False (complete)
        mock_is_running.side_effect = [False, True, False]

        config = _make_config()
        orch = Orchestrator(config)

        # Simulate shutdown after first job
        original_execute = orch._execute_job

        def execute_then_shutdown(job):
            result = original_execute(job)
            orch._shutdown_requested = True
            return result

        with patch.object(orch, "_execute_job", side_effect=execute_then_shutdown):
            result = orch.run()

        # First job completed, rest skipped
        self.assertEqual(result.total, 3)
        self.assertEqual(result.completed, 1)
        self.assertEqual(result.skipped, 2)

    def test_signal_handler(self):
        config = _make_config()
        orch = Orchestrator(config)
        self.assertFalse(orch._shutdown_requested)
        orch._handle_signal(2, None)  # SIGINT
        self.assertTrue(orch._shutdown_requested)


class TestPollErrors(unittest.TestCase):

    @patch("jetbackup_remote.orchestrator.is_job_running")
    @patch("jetbackup_remote.orchestrator.run_job")
    @patch("jetbackup_remote.orchestrator.test_connection")
    def test_poll_error_retries(self, mock_test, mock_run, mock_is_running):
        """SSH error during completion poll is retried."""
        mock_test.return_value = {"ssh_ok": True, "jetbackup_ok": True, "error": None}
        mock_run.return_value = True
        # 1: not running (check) → trigger
        # 2: running (startup) → started
        # 3: error (completion poll) → retry
        # 4: not running (completion poll) → done
        mock_is_running.side_effect = [
            False,
            True,
            SSHError("temporary failure"),
            False,
        ]

        config = _make_config(jobs=[
            Job(job_id="job1", server_name="srv1", label="Backup1"),
        ])
        config.orchestrator.poll_interval = 0.01
        orch = Orchestrator(config)
        result = orch.run()

        self.assertEqual(result.completed, 1)

    @patch("jetbackup_remote.orchestrator.is_job_running")
    @patch("jetbackup_remote.orchestrator.run_job")
    @patch("jetbackup_remote.orchestrator.test_connection")
    def test_startup_error_retries(self, mock_test, mock_run, mock_is_running):
        """SSH error during startup wait is retried."""
        mock_test.return_value = {"ssh_ok": True, "jetbackup_ok": True, "error": None}
        mock_run.return_value = True
        # 1: not running (check) → trigger
        # 2: error (startup) → retry
        # 3: running (startup) → started
        # 4: not running (completion) → done
        mock_is_running.side_effect = [
            False,
            SSHError("temporary failure"),
            True,
            False,
        ]

        config = _make_config(jobs=[
            Job(job_id="job1", server_name="srv1", label="Backup1"),
        ])
        config.orchestrator.poll_interval = 0.01
        orch = Orchestrator(config)
        result = orch.run()

        self.assertEqual(result.completed, 1)

    @patch("jetbackup_remote.orchestrator.is_job_running")
    @patch("jetbackup_remote.orchestrator.test_connection")
    def test_status_check_failure(self, mock_test, mock_is_running):
        mock_test.return_value = {"ssh_ok": True, "jetbackup_ok": True, "error": None}
        mock_is_running.side_effect = JetBackupAPIError("API error")

        config = _make_config(jobs=[
            Job(job_id="job1", server_name="srv1", label="Backup1"),
        ])
        orch = Orchestrator(config)
        result = orch.run()

        self.assertEqual(result.failed, 1)


if __name__ == "__main__":
    unittest.main()
