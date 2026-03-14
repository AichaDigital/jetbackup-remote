"""Tests for jetbackup_remote.orchestrator."""

import json
import os
import tempfile
import time
import unittest
import unittest.mock
from unittest.mock import patch, MagicMock, call

from jetbackup_remote.config import Config, DestinationConfig, OrchestratorConfig, NotificationConfig, LoopConfig, load_config
from jetbackup_remote.models import Job, JobRun, JobStatus, JobType, QueueGroupStatus, Server, ServerRun, RunResult
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


def _make_config_with_dest(jobs=None, servers=None):
    """Create a test Config with destination_id on servers."""
    if servers is None:
        servers = {
            "srv1": Server(name="srv1", host="srv1.example.com", destination_id="dest1"),
        }
    if jobs is None:
        jobs = [
            Job(job_id="job1", server_name="srv1", label="Backup1"),
        ]

    fd, lock_path = tempfile.mkstemp()
    os.close(fd)
    os.unlink(lock_path)

    return Config(
        servers=servers,
        jobs=jobs,
        orchestrator=OrchestratorConfig(
            poll_interval=0.01,
            job_timeout=1,
            startup_timeout=0.5,
            lock_file=lock_path,
        ),
        notification=NotificationConfig(),
        destination=DestinationConfig(force_activate=True, skip_if_disabled=False),
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

    @patch("jetbackup_remote.orchestrator.list_queue_groups", return_value=[])
    @patch("jetbackup_remote.orchestrator.set_job_enabled", return_value=True)
    @patch("jetbackup_remote.orchestrator.is_job_running")
    @patch("jetbackup_remote.orchestrator.run_job")
    @patch("jetbackup_remote.orchestrator.test_connection")
    def test_successful_execution(self, mock_test, mock_run, mock_is_running, mock_set_enabled, mock_list_qg):
        """Trigger → startup wait sees running=True → poll sees running=False."""
        mock_test.return_value = {"ssh_ok": True, "jetbackup_ok": True, "error": None}
        mock_run.return_value = True
        # Preflight: False (job1 on srv1), False (job1 on srv2 — no jobs but server exists)
        # Execution: not running (check) → trigger → running (startup) → not running (done)
        mock_is_running.side_effect = [False, False, True, False]

        config = _make_config(jobs=[
            Job(job_id="job1", server_name="srv1", label="Backup1"),
        ])
        orch = Orchestrator(config)
        result = orch.run()

        self.assertEqual(result.total, 1)
        self.assertEqual(result.completed, 1)
        mock_run.assert_called_once()

    @patch("jetbackup_remote.orchestrator.list_queue_groups", return_value=[])
    @patch("jetbackup_remote.orchestrator.set_job_enabled", return_value=True)
    @patch("jetbackup_remote.orchestrator.is_job_running")
    @patch("jetbackup_remote.orchestrator.run_job")
    @patch("jetbackup_remote.orchestrator.test_connection")
    def test_job_already_running(self, mock_test, mock_run, mock_is_running, mock_set_enabled, mock_list_qg):
        """Already running → skip trigger and startup, go to completion poll."""
        mock_test.return_value = {"ssh_ok": True, "jetbackup_ok": True, "error": None}
        # Preflight: False (job1 on srv1 — not running during preflight)
        # Execution: already running (check) → skip trigger → not running (done)
        mock_is_running.side_effect = [False, True, False]

        config = _make_config(jobs=[
            Job(job_id="job1", server_name="srv1", label="Backup1"),
        ])
        orch = Orchestrator(config)
        result = orch.run()

        self.assertEqual(result.completed, 1)
        mock_run.assert_not_called()  # Should not trigger if already running

    @patch("jetbackup_remote.orchestrator.list_queue_groups", return_value=[])
    @patch("jetbackup_remote.orchestrator.is_job_running", return_value=False)
    @patch("jetbackup_remote.orchestrator.test_connection")
    def test_ssh_unreachable_skips(self, mock_test, mock_is_running, mock_list_qg):
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

    @patch("jetbackup_remote.orchestrator.list_queue_groups", return_value=[])
    @patch("jetbackup_remote.orchestrator.set_job_enabled", return_value=True)
    @patch("jetbackup_remote.orchestrator.is_job_running")
    @patch("jetbackup_remote.orchestrator.run_job")
    @patch("jetbackup_remote.orchestrator.test_connection")
    def test_trigger_failure(self, mock_test, mock_run, mock_is_running, mock_set_enabled, mock_list_qg):
        mock_test.return_value = {"ssh_ok": True, "jetbackup_ok": True, "error": None}
        # Preflight: False, then execution: False (check) → trigger fails
        mock_is_running.side_effect = [False, False]
        mock_run.side_effect = JetBackupAPIError("API error")

        config = _make_config(jobs=[
            Job(job_id="job1", server_name="srv1", label="Backup1"),
        ])
        orch = Orchestrator(config)
        result = orch.run()

        self.assertEqual(result.total, 1)
        self.assertEqual(result.failed, 1)

    @patch("jetbackup_remote.orchestrator.stop_queue_group")
    @patch("jetbackup_remote.orchestrator.list_queue_groups")
    @patch("jetbackup_remote.orchestrator.set_job_enabled", return_value=True)
    @patch("jetbackup_remote.orchestrator.is_job_running")
    @patch("jetbackup_remote.orchestrator.run_job")
    @patch("jetbackup_remote.orchestrator.test_connection")
    def test_timeout_aborts_job(self, mock_test, mock_run, mock_is_running,
                                mock_set_enabled, mock_list_qg, mock_stop):
        """Job starts but never finishes → timeout triggers abort via stopQueueGroup."""
        mock_test.return_value = {"ssh_ok": True, "jetbackup_ok": True, "error": None}
        mock_run.return_value = True
        # list_queue_groups calls:
        # 1. preflight srv1 → empty
        # 2. preflight srv2 → empty
        # 3. abort: _find_running_queue_group_for_job → running group
        mock_list_qg.side_effect = [
            [],       # preflight srv1
            [],       # preflight srv2
            [{"_id": "qg1", "status": 2, "data": {"_id": "job1"}}],  # abort lookup
        ]
        # is_job_running calls:
        # 1. preflight: job1 on srv1 → False
        # 2. execution: initial check → False (trigger)
        # 3. execution: startup → True (started)
        # 4-N. execution: completion poll → True (timeout)
        # N+1. abort: confirmation → False (stopped)
        mock_is_running.side_effect = [
            False,    # preflight
            False,    # initial check
            True,     # startup
        ] + [True] * 50 + [
            False,    # abort confirmation
        ]
        mock_stop.return_value = True

        config = _make_config(jobs=[
            Job(job_id="job1", server_name="srv1", label="Backup1"),
        ])
        config.orchestrator.job_timeout = 0.05  # 50ms timeout
        config.orchestrator.poll_interval = 0.01

        orch = Orchestrator(config)
        orch.ABORT_WAIT_TIMEOUT = 2
        orch.ABORT_POLL_INTERVAL = 0.01
        result = orch.run()

        self.assertEqual(result.total, 1)
        self.assertEqual(result.timed_out, 1)
        self.assertFalse(result.success)
        # stop_queue_group was called to abort
        mock_stop.assert_called()
        # Verify the timeout error message reflects abort
        job_run = result.jobs[0]
        self.assertIn("aborted", job_run.error_message)

    @patch("jetbackup_remote.orchestrator.list_queue_groups", return_value=[])
    @patch("jetbackup_remote.orchestrator.set_job_enabled", return_value=True)
    @patch("jetbackup_remote.orchestrator.is_job_running")
    @patch("jetbackup_remote.orchestrator.run_job")
    @patch("jetbackup_remote.orchestrator.test_connection")
    def test_startup_timeout_fails(self, mock_test, mock_run, mock_is_running, mock_set_enabled, mock_list_qg):
        """Job triggered but never starts within startup_timeout → fail."""
        mock_test.return_value = {"ssh_ok": True, "jetbackup_ok": True, "error": None}
        mock_run.return_value = True
        # All False: preflight check + execution check + startup polls
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

    @patch("jetbackup_remote.orchestrator.list_queue_groups", return_value=[])
    @patch("jetbackup_remote.orchestrator.set_job_enabled", return_value=True)
    @patch("jetbackup_remote.orchestrator.is_job_running")
    @patch("jetbackup_remote.orchestrator.run_job")
    @patch("jetbackup_remote.orchestrator.test_connection")
    def test_multiple_jobs_sequential(self, mock_test, mock_run, mock_is_running, mock_set_enabled, mock_list_qg):
        """All 3 jobs run sequentially: check→trigger→startup→complete each."""
        mock_test.return_value = {"ssh_ok": True, "jetbackup_ok": True, "error": None}
        mock_run.return_value = True
        # Preflight: False for each managed job (job1, job2 on srv1 + job3 on srv2)
        # Execution: each job: False (check) → True (startup) → False (complete)
        mock_is_running.side_effect = [
            False, False, False,  # preflight: job1, job2 on srv1 + job3 on srv2
            False, True, False,   # job1
            False, True, False,   # job2
            False, True, False,   # job3
        ]

        config = _make_config()
        orch = Orchestrator(config)
        result = orch.run()

        self.assertEqual(result.total, 3)
        self.assertEqual(result.completed, 3)
        self.assertEqual(mock_run.call_count, 3)


class TestShutdown(unittest.TestCase):

    @patch("jetbackup_remote.orchestrator.list_queue_groups", return_value=[])
    @patch("jetbackup_remote.orchestrator.set_job_enabled", return_value=True)
    @patch("jetbackup_remote.orchestrator.is_job_running")
    @patch("jetbackup_remote.orchestrator.run_job")
    @patch("jetbackup_remote.orchestrator.test_connection")
    def test_shutdown_skips_remaining(self, mock_test, mock_run, mock_is_running, mock_set_enabled, mock_list_qg):
        mock_test.return_value = {"ssh_ok": True, "jetbackup_ok": True, "error": None}
        mock_run.return_value = True
        # Preflight: False for all 3 jobs
        # First job: False (check) → True (startup) → False (complete)
        mock_is_running.side_effect = [False, False, False, False, True, False]

        config = _make_config()
        orch = Orchestrator(config)

        # Simulate shutdown after first job on any server
        original_execute = orch._execute_job_with_lifecycle

        def execute_then_shutdown(server, job):
            result = original_execute(server, job)
            orch._shutdown_requested = True
            return result

        with patch.object(orch, "_execute_job_with_lifecycle", side_effect=execute_then_shutdown):
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

    @patch("jetbackup_remote.orchestrator.list_queue_groups", return_value=[])
    @patch("jetbackup_remote.orchestrator.set_job_enabled", return_value=True)
    @patch("jetbackup_remote.orchestrator.is_job_running")
    @patch("jetbackup_remote.orchestrator.run_job")
    @patch("jetbackup_remote.orchestrator.test_connection")
    def test_poll_error_retries(self, mock_test, mock_run, mock_is_running, mock_set_enabled, mock_list_qg):
        """SSH error during completion poll is retried."""
        mock_test.return_value = {"ssh_ok": True, "jetbackup_ok": True, "error": None}
        mock_run.return_value = True
        # Preflight: False (job1)
        # Execution: not running (check) → trigger → running (startup) →
        # error (poll retry) → not running (done)
        mock_is_running.side_effect = [
            False,  # preflight
            False,  # check
            True,   # startup
            SSHError("temporary failure"),  # poll error
            False,  # poll done
        ]

        config = _make_config(jobs=[
            Job(job_id="job1", server_name="srv1", label="Backup1"),
        ])
        config.orchestrator.poll_interval = 0.01
        orch = Orchestrator(config)
        result = orch.run()

        self.assertEqual(result.completed, 1)

    @patch("jetbackup_remote.orchestrator.list_queue_groups", return_value=[])
    @patch("jetbackup_remote.orchestrator.set_job_enabled", return_value=True)
    @patch("jetbackup_remote.orchestrator.is_job_running")
    @patch("jetbackup_remote.orchestrator.run_job")
    @patch("jetbackup_remote.orchestrator.test_connection")
    def test_startup_error_retries(self, mock_test, mock_run, mock_is_running, mock_set_enabled, mock_list_qg):
        """SSH error during startup wait is retried."""
        mock_test.return_value = {"ssh_ok": True, "jetbackup_ok": True, "error": None}
        mock_run.return_value = True
        # Preflight: False (job1)
        # Execution: not running (check) → trigger → error (startup retry) →
        # running (startup) → not running (done)
        mock_is_running.side_effect = [
            False,  # preflight
            False,  # check
            SSHError("temporary failure"),  # startup error
            True,   # startup ok
            False,  # completion
        ]

        config = _make_config(jobs=[
            Job(job_id="job1", server_name="srv1", label="Backup1"),
        ])
        config.orchestrator.poll_interval = 0.01
        orch = Orchestrator(config)
        result = orch.run()

        self.assertEqual(result.completed, 1)

    @patch("jetbackup_remote.orchestrator.list_queue_groups", return_value=[])
    @patch("jetbackup_remote.orchestrator.set_job_enabled", return_value=True)
    @patch("jetbackup_remote.orchestrator.is_job_running")
    @patch("jetbackup_remote.orchestrator.test_connection")
    def test_status_check_failure(self, mock_test, mock_is_running, mock_set_enabled, mock_list_qg):
        mock_test.return_value = {"ssh_ok": True, "jetbackup_ok": True, "error": None}
        # Preflight: False (job1), then execution check: API error
        mock_is_running.side_effect = [False, JetBackupAPIError("API error")]

        config = _make_config(jobs=[
            Job(job_id="job1", server_name="srv1", label="Backup1"),
        ])
        orch = Orchestrator(config)
        result = orch.run()

        self.assertEqual(result.failed, 1)


class TestBuildServerGroups(unittest.TestCase):

    def test_groups_by_server(self):
        config = _make_config()
        orch = Orchestrator(config)
        groups = orch._build_server_groups()
        self.assertIn("srv1", groups)
        self.assertIn("srv2", groups)
        self.assertEqual(len(groups["srv1"]), 2)
        self.assertEqual(len(groups["srv2"]), 1)

    def test_groups_with_server_filter(self):
        config = _make_config()
        orch = Orchestrator(config)
        groups = orch._build_server_groups(server_filter="srv1")
        self.assertIn("srv1", groups)
        self.assertNotIn("srv2", groups)

    def test_groups_with_job_filter(self):
        config = _make_config()
        orch = Orchestrator(config)
        groups = orch._build_server_groups(job_filter="job3")
        self.assertNotIn("srv1", groups)
        self.assertIn("srv2", groups)
        self.assertEqual(len(groups["srv2"]), 1)

    def test_groups_empty(self):
        config = _make_config()
        orch = Orchestrator(config)
        groups = orch._build_server_groups(server_filter="nonexistent")
        self.assertEqual(len(groups), 0)


class TestDestinationLifecycle(unittest.TestCase):

    @patch("jetbackup_remote.orchestrator.set_destination_state")
    def test_activate_success(self, mock_set_state):
        mock_set_state.return_value = True
        config = _make_config_with_dest()
        orch = Orchestrator(config)
        server = config.servers["srv1"]
        sr = ServerRun(server_name="srv1", destination_id="dest1")
        result = orch._activate_destination(server, sr)
        self.assertTrue(result)
        self.assertTrue(sr.destination_activated)
        mock_set_state.assert_called_once_with(
            server, "dest1", disabled=False, ssh_key=None,
        )

    @patch("jetbackup_remote.orchestrator.set_destination_state")
    def test_activate_failure(self, mock_set_state):
        mock_set_state.side_effect = JetBackupAPIError("API error")
        config = _make_config_with_dest()
        orch = Orchestrator(config)
        server = config.servers["srv1"]
        sr = ServerRun(server_name="srv1", destination_id="dest1")
        result = orch._activate_destination(server, sr)
        self.assertFalse(result)
        self.assertFalse(sr.destination_activated)
        self.assertIsNotNone(sr.activation_error)

    @patch("jetbackup_remote.orchestrator.set_destination_state")
    def test_deactivate_success(self, mock_set_state):
        mock_set_state.return_value = True
        config = _make_config_with_dest()
        orch = Orchestrator(config)
        server = config.servers["srv1"]
        sr = ServerRun(server_name="srv1", destination_id="dest1", destination_activated=True)
        result = orch._deactivate_destination(server, sr)
        self.assertTrue(result)
        self.assertTrue(sr.destination_deactivated)

    @patch("jetbackup_remote.orchestrator.set_destination_state")
    def test_deactivate_failure_critical(self, mock_set_state):
        mock_set_state.side_effect = JetBackupAPIError("API error")
        config = _make_config_with_dest()
        orch = Orchestrator(config)
        server = config.servers["srv1"]
        sr = ServerRun(server_name="srv1", destination_id="dest1", destination_activated=True)
        result = orch._deactivate_destination(server, sr)
        self.assertFalse(result)
        self.assertIsNotNone(sr.deactivation_error)
        self.assertIn("CRITICAL", sr.deactivation_error)

    def test_deactivate_skips_if_not_activated(self):
        config = _make_config_with_dest()
        orch = Orchestrator(config)
        server = config.servers["srv1"]
        sr = ServerRun(server_name="srv1", destination_id="dest1", destination_activated=False)
        result = orch._deactivate_destination(server, sr)
        self.assertTrue(result)

    def test_activate_skips_no_destination_id(self):
        config = _make_config()
        orch = Orchestrator(config)
        server = config.servers["srv1"]
        sr = ServerRun(server_name="srv1")
        result = orch._activate_destination(server, sr)
        self.assertTrue(result)


class TestJobLifecycle(unittest.TestCase):

    @patch("jetbackup_remote.orchestrator.set_job_enabled")
    def test_enable_success(self, mock_set):
        mock_set.return_value = True
        config = _make_config()
        orch = Orchestrator(config)
        server = config.servers["srv1"]
        job = config.jobs[0]
        result = orch._enable_job(server, job)
        self.assertTrue(result)
        mock_set.assert_called_once_with(server, job.job_id, enabled=True, ssh_key=None)

    @patch("jetbackup_remote.orchestrator.set_job_enabled")
    def test_enable_failure(self, mock_set):
        mock_set.side_effect = JetBackupAPIError("API error")
        config = _make_config()
        orch = Orchestrator(config)
        server = config.servers["srv1"]
        job = config.jobs[0]
        result = orch._enable_job(server, job)
        self.assertFalse(result)

    @patch("jetbackup_remote.orchestrator.set_job_enabled")
    def test_disable_success(self, mock_set):
        mock_set.return_value = True
        config = _make_config()
        orch = Orchestrator(config)
        server = config.servers["srv1"]
        job = config.jobs[0]
        result = orch._disable_job(server, job)
        self.assertTrue(result)

    @patch("jetbackup_remote.orchestrator.set_job_enabled")
    def test_disable_failure(self, mock_set):
        mock_set.side_effect = JetBackupAPIError("API error")
        config = _make_config()
        orch = Orchestrator(config)
        server = config.servers["srv1"]
        job = config.jobs[0]
        result = orch._disable_job(server, job)
        self.assertFalse(result)


class TestQueueCleanCheck(unittest.TestCase):

    @patch("jetbackup_remote.orchestrator.list_queue_groups")
    def test_clean_queue(self, mock_list):
        mock_list.return_value = []
        config = _make_config_with_dest()
        orch = Orchestrator(config)
        server = config.servers["srv1"]
        is_clean, killed = orch._check_queue_clean(server, "dest1")
        self.assertTrue(is_clean)
        self.assertEqual(killed, 0)

    @patch("jetbackup_remote.orchestrator.stop_queue_group")
    @patch("jetbackup_remote.orchestrator.list_queue_groups")
    def test_orphan_killed(self, mock_list, mock_stop):
        mock_list.return_value = [{"_id": "orphan1", "status": 2}]
        mock_stop.return_value = True
        config = _make_config_with_dest()
        orch = Orchestrator(config)
        server = config.servers["srv1"]
        is_clean, killed = orch._check_queue_clean(server, "dest1")
        self.assertTrue(is_clean)
        self.assertEqual(killed, 1)
        mock_stop.assert_called_once()

    @patch("jetbackup_remote.orchestrator.list_queue_groups")
    def test_api_error_returns_clean(self, mock_list):
        mock_list.side_effect = JetBackupAPIError("error")
        config = _make_config_with_dest()
        orch = Orchestrator(config)
        server = config.servers["srv1"]
        is_clean, killed = orch._check_queue_clean(server, "dest1")
        self.assertTrue(is_clean)
        self.assertEqual(killed, 0)


class TestVerifyJobOutcome(unittest.TestCase):

    @patch("jetbackup_remote.orchestrator.list_queue_groups")
    def test_no_queue_group_found(self, mock_list):
        mock_list.return_value = []
        config = _make_config()
        orch = Orchestrator(config)
        server = config.servers["srv1"]
        job = config.jobs[0]
        jr = JobRun(job=job)
        orch._verify_job_outcome(server, job, jr)
        self.assertIsNone(jr.queue_group_id)
        self.assertIsNone(jr.queue_group_status)

    @patch("jetbackup_remote.orchestrator.list_queue_groups")
    def test_completed_status(self, mock_list):
        mock_list.return_value = [
            {"_id": "qg1", "status": 100, "data": {"_id": "job1", "name": "Test"}},
        ]
        config = _make_config()
        orch = Orchestrator(config)
        server = config.servers["srv1"]
        job = config.jobs[0]
        jr = JobRun(job=job)
        orch._verify_job_outcome(server, job, jr)
        self.assertEqual(jr.queue_group_id, "qg1")
        self.assertEqual(jr.queue_group_status, QueueGroupStatus.COMPLETED)

    @patch("jetbackup_remote.orchestrator.get_queue_group")
    @patch("jetbackup_remote.orchestrator.list_queue_groups")
    def test_partial_fetches_logs(self, mock_list, mock_get):
        mock_list.return_value = [
            {"_id": "qg1", "status": 102, "data": {"_id": "job1", "name": "Test"}},
        ]
        mock_get.return_value = {
            "_id": "qg1", "status": 102, "log_contents": "Partial: 3/5",
        }
        config = _make_config()
        orch = Orchestrator(config)
        server = config.servers["srv1"]
        job = config.jobs[0]
        jr = JobRun(job=job)
        orch._verify_job_outcome(server, job, jr)
        self.assertEqual(jr.queue_group_status, QueueGroupStatus.PARTIAL)
        self.assertEqual(jr.log_contents, "Partial: 3/5")
        mock_get.assert_called_once()


class TestSchedulerInterference(unittest.TestCase):

    @patch("jetbackup_remote.orchestrator.list_queue_groups")
    def test_no_interference(self, mock_list):
        mock_list.return_value = []
        config = _make_config_with_dest()
        orch = Orchestrator(config)
        server = config.servers["srv1"]
        foreign = orch._check_scheduler_interference(server, "dest1", set())
        self.assertEqual(foreign, [])

    @patch("jetbackup_remote.orchestrator.stop_queue_group")
    @patch("jetbackup_remote.orchestrator.list_queue_groups")
    def test_foreign_group_stopped(self, mock_list, mock_stop):
        mock_list.return_value = [
            {"_id": "foreign1", "status": 2},
        ]
        mock_stop.return_value = True
        config = _make_config_with_dest()
        orch = Orchestrator(config)
        server = config.servers["srv1"]
        foreign = orch._check_scheduler_interference(server, "dest1", set())
        self.assertEqual(foreign, ["foreign1"])
        mock_stop.assert_called_once()

    @patch("jetbackup_remote.orchestrator.list_queue_groups")
    def test_own_group_ignored(self, mock_list):
        mock_list.return_value = [
            {"_id": "ours", "status": 2},
        ]
        config = _make_config_with_dest()
        orch = Orchestrator(config)
        server = config.servers["srv1"]
        foreign = orch._check_scheduler_interference(server, "dest1", {"ours"})
        self.assertEqual(foreign, [])


class TestProcessServer(unittest.TestCase):

    @patch("jetbackup_remote.orchestrator.list_queue_groups", return_value=[])
    @patch("jetbackup_remote.orchestrator.set_destination_state", return_value=True)
    @patch("jetbackup_remote.orchestrator.set_job_enabled", return_value=True)
    @patch("jetbackup_remote.orchestrator.is_job_running")
    @patch("jetbackup_remote.orchestrator.run_job", return_value=True)
    @patch("jetbackup_remote.orchestrator.is_destination_disabled", return_value=True)
    @patch("jetbackup_remote.orchestrator.test_connection")
    def test_full_lifecycle(self, mock_test, mock_dest_dis, mock_run, mock_running,
                            mock_set_enabled, mock_set_dest, mock_list_qg):
        """Full lifecycle: activate dest -> enable job -> run -> disable job -> deactivate dest."""
        mock_test.return_value = {"ssh_ok": True, "jetbackup_ok": True, "error": None}
        mock_running.side_effect = [False, True, False]

        config = _make_config_with_dest()
        orch = Orchestrator(config)
        server = config.servers["srv1"]
        jobs = [config.jobs[0]]
        sr = orch._process_server(server, jobs, force_activate=True)

        # Destination activated and deactivated
        self.assertTrue(sr.destination_activated)
        self.assertTrue(sr.destination_deactivated)
        self.assertIsNone(sr.deactivation_error)

        # Job completed
        self.assertEqual(len(sr.job_runs), 1)
        self.assertEqual(sr.job_runs[0].status, JobStatus.COMPLETED)

        # set_job_enabled called for enable+disable
        self.assertEqual(mock_set_enabled.call_count, 2)

        # set_destination_state called for activate+deactivate
        self.assertEqual(mock_set_dest.call_count, 2)

    @patch("jetbackup_remote.orchestrator.test_connection")
    def test_ssh_unreachable_skips_all(self, mock_test):
        mock_test.return_value = {"ssh_ok": False, "jetbackup_ok": False, "error": "refused"}

        config = _make_config_with_dest()
        orch = Orchestrator(config)
        server = config.servers["srv1"]
        jobs = [config.jobs[0]]
        sr = orch._process_server(server, jobs)

        self.assertEqual(len(sr.job_runs), 1)
        self.assertEqual(sr.job_runs[0].status, JobStatus.SKIPPED)

    @patch("jetbackup_remote.orchestrator.is_destination_disabled", return_value=True)
    @patch("jetbackup_remote.orchestrator.test_connection")
    def test_skip_if_disabled(self, mock_test, mock_dest_dis):
        """With skip_if_disabled=True and destination disabled, skip all jobs."""
        mock_test.return_value = {"ssh_ok": True, "jetbackup_ok": True, "error": None}

        config = _make_config_with_dest()
        config.destination = DestinationConfig(skip_if_disabled=True, force_activate=False)
        orch = Orchestrator(config)
        server = config.servers["srv1"]
        jobs = [config.jobs[0]]
        sr = orch._process_server(server, jobs, force_activate=False)

        self.assertEqual(len(sr.job_runs), 1)
        self.assertEqual(sr.job_runs[0].status, JobStatus.SKIPPED)
        self.assertIn("disabled", sr.job_runs[0].error_message)


class TestFindRunningQueueGroup(unittest.TestCase):

    @patch("jetbackup_remote.orchestrator.list_queue_groups")
    def test_finds_running_group(self, mock_list):
        mock_list.return_value = [
            {"_id": "qg1", "status": 2, "data": {"_id": "job1", "name": "Test"}},
        ]
        config = _make_config()
        orch = Orchestrator(config)
        server = config.servers["srv1"]
        job = config.jobs[0]
        group = orch._find_running_queue_group_for_job(server, job)
        self.assertIsNotNone(group)
        self.assertEqual(group["_id"], "qg1")

    @patch("jetbackup_remote.orchestrator.list_queue_groups")
    def test_ignores_completed_groups(self, mock_list):
        mock_list.return_value = [
            {"_id": "qg1", "status": 100, "data": {"_id": "job1", "name": "Test"}},
        ]
        config = _make_config()
        orch = Orchestrator(config)
        server = config.servers["srv1"]
        job = config.jobs[0]
        group = orch._find_running_queue_group_for_job(server, job)
        self.assertIsNone(group)

    @patch("jetbackup_remote.orchestrator.list_queue_groups")
    def test_ignores_different_job(self, mock_list):
        mock_list.return_value = [
            {"_id": "qg1", "status": 2, "data": {"_id": "other_job", "name": "Other"}},
        ]
        config = _make_config()
        orch = Orchestrator(config)
        server = config.servers["srv1"]
        job = config.jobs[0]
        group = orch._find_running_queue_group_for_job(server, job)
        self.assertIsNone(group)

    @patch("jetbackup_remote.orchestrator.list_queue_groups")
    def test_api_error_returns_none(self, mock_list):
        mock_list.side_effect = JetBackupAPIError("error")
        config = _make_config()
        orch = Orchestrator(config)
        server = config.servers["srv1"]
        job = config.jobs[0]
        group = orch._find_running_queue_group_for_job(server, job)
        self.assertIsNone(group)


class TestAbortRunningJob(unittest.TestCase):

    @patch("jetbackup_remote.orchestrator.is_job_running")
    @patch("jetbackup_remote.orchestrator.stop_queue_group")
    @patch("jetbackup_remote.orchestrator.list_queue_groups")
    def test_abort_success(self, mock_list, mock_stop, mock_running):
        """Abort finds queue group, stops it, confirms job stopped."""
        mock_list.return_value = [
            {"_id": "qg1", "status": 2, "data": {"_id": "job1"}},
        ]
        mock_stop.return_value = True
        mock_running.return_value = False  # Job confirmed stopped

        config = _make_config()
        orch = Orchestrator(config)
        orch.ABORT_WAIT_TIMEOUT = 0.5
        orch.ABORT_POLL_INTERVAL = 0.1
        server = config.servers["srv1"]
        job = config.jobs[0]
        jr = JobRun(job=job)

        result = orch._abort_running_job(server, job, jr)

        self.assertTrue(result)
        mock_stop.assert_called_once()

    @patch("jetbackup_remote.orchestrator.is_job_running")
    @patch("jetbackup_remote.orchestrator.list_queue_groups")
    def test_abort_no_queue_group_but_job_stops(self, mock_list, mock_running):
        """No running queue group found, but job stops on its own."""
        mock_list.return_value = []
        mock_running.return_value = False

        config = _make_config()
        orch = Orchestrator(config)
        orch.ABORT_WAIT_TIMEOUT = 0.5
        orch.ABORT_POLL_INTERVAL = 0.1
        server = config.servers["srv1"]
        job = config.jobs[0]
        jr = JobRun(job=job)

        result = orch._abort_running_job(server, job, jr)
        self.assertTrue(result)

    @patch("jetbackup_remote.orchestrator.is_job_running")
    @patch("jetbackup_remote.orchestrator.stop_queue_group")
    @patch("jetbackup_remote.orchestrator.list_queue_groups")
    def test_abort_stop_fails(self, mock_list, mock_stop, mock_running):
        """stop_queue_group raises error → returns False without blocking."""
        mock_list.return_value = [
            {"_id": "qg1", "status": 2, "data": {"_id": "job1"}},
        ]
        mock_stop.side_effect = JetBackupAPIError("API error")

        config = _make_config()
        orch = Orchestrator(config)
        orch.ABORT_WAIT_TIMEOUT = 0.5
        orch.ABORT_POLL_INTERVAL = 0.1
        server = config.servers["srv1"]
        job = config.jobs[0]
        jr = JobRun(job=job)

        result = orch._abort_running_job(server, job, jr)
        self.assertFalse(result)

    @patch("jetbackup_remote.orchestrator.is_job_running")
    @patch("jetbackup_remote.orchestrator.stop_queue_group")
    @patch("jetbackup_remote.orchestrator.list_queue_groups")
    def test_abort_timeout_still_running(self, mock_list, mock_stop, mock_running):
        """Job never stops within ABORT_WAIT_TIMEOUT → returns False."""
        mock_list.return_value = [
            {"_id": "qg1", "status": 2, "data": {"_id": "job1"}},
        ]
        mock_stop.return_value = True
        mock_running.return_value = True  # Never stops

        config = _make_config()
        orch = Orchestrator(config)
        orch.ABORT_WAIT_TIMEOUT = 0.2
        orch.ABORT_POLL_INTERVAL = 0.05
        server = config.servers["srv1"]
        job = config.jobs[0]
        jr = JobRun(job=job)

        result = orch._abort_running_job(server, job, jr)
        self.assertFalse(result)


class TestGlobalPreflightCheck(unittest.TestCase):

    @patch("jetbackup_remote.orchestrator.is_job_running")
    @patch("jetbackup_remote.orchestrator.list_queue_groups")
    def test_clean_servers(self, mock_list, mock_running):
        """No running backups → preflight passes."""
        mock_list.return_value = []
        mock_running.return_value = False

        config = _make_config()
        orch = Orchestrator(config)
        result = orch._global_preflight_check()
        self.assertTrue(result)

    @patch("jetbackup_remote.orchestrator.is_job_running")
    @patch("jetbackup_remote.orchestrator.stop_queue_group")
    @patch("jetbackup_remote.orchestrator.list_queue_groups")
    def test_running_groups_stopped(self, mock_list, mock_stop, mock_running):
        """Running queue groups found and successfully stopped → passes."""
        mock_list.return_value = [
            {"_id": "qg1", "status": 2},
        ]
        mock_stop.return_value = True
        mock_running.return_value = False

        config = _make_config()
        orch = Orchestrator(config)
        result = orch._global_preflight_check()
        self.assertTrue(result)
        mock_stop.assert_called()

    @patch("jetbackup_remote.orchestrator.is_job_running")
    @patch("jetbackup_remote.orchestrator.list_queue_groups")
    def test_managed_job_still_running(self, mock_list, mock_running):
        """A managed job is still running → preflight fails."""
        mock_list.return_value = []
        # srv1 has job1, job2 → first True, rest False
        # srv2 has job3 → False
        mock_running.side_effect = [True, False, False]

        config = _make_config()
        orch = Orchestrator(config)
        result = orch._global_preflight_check()
        self.assertFalse(result)

    @patch("jetbackup_remote.orchestrator.is_job_running")
    @patch("jetbackup_remote.orchestrator.list_queue_groups")
    def test_unreachable_server_skipped(self, mock_list, mock_running):
        """Server unreachable during preflight → skipped, not treated as failure."""
        mock_list.side_effect = SSHError("Connection refused")

        config = _make_config()
        orch = Orchestrator(config)
        result = orch._global_preflight_check()
        # Unreachable servers are skipped, not failure
        self.assertTrue(result)
        mock_running.assert_not_called()

    @patch("jetbackup_remote.orchestrator.is_job_running")
    @patch("jetbackup_remote.orchestrator.stop_queue_group")
    @patch("jetbackup_remote.orchestrator.list_queue_groups")
    def test_stop_fails_returns_false(self, mock_list, mock_stop, mock_running):
        """stop_queue_group fails → preflight fails."""
        mock_list.return_value = [
            {"_id": "qg1", "status": 2},
        ]
        mock_stop.side_effect = JetBackupAPIError("API error")
        mock_running.return_value = False

        config = _make_config()
        orch = Orchestrator(config)
        result = orch._global_preflight_check()
        self.assertFalse(result)


class TestOrchestratorV030(unittest.TestCase):
    """Tests for v0.3.0 orchestrator features."""

    def _make_config(self):
        """Create minimal Config for testing."""
        return Config(
            servers={"srv1": Server(name="srv1", host="test.com", destination_id="d1")},
            jobs=[Job(job_id="j1", server_name="srv1", label="TestJob")],
            orchestrator=OrchestratorConfig(
                job_timeout=86400,
                state_file="/tmp/test-jetbackup-state.json",
            ),
            notification=NotificationConfig(enabled=False),
            destination=DestinationConfig(),
            loop=LoopConfig(target_interval=100, min_pause=10),
        )

    def test_per_job_timeout_used(self):
        config = self._make_config()
        orch = Orchestrator(config)
        job = Job(job_id="j1", server_name="srv1", label="FastJob", timeout=3600)
        server = config.servers["srv1"]
        job_run = JobRun(job=job)
        job_run.start()
        with unittest.mock.patch(
            "jetbackup_remote.orchestrator.is_job_running", return_value=False
        ):
            orch._poll_completion_only(server, job, job_run)
        self.assertEqual(job_run.status, JobStatus.COMPLETED)

    def test_per_job_timeout_fallback_to_global(self):
        config = self._make_config()
        config.orchestrator.job_timeout = 5  # very short for test
        orch = Orchestrator(config)
        job = Job(job_id="j1", server_name="srv1", label="NoTimeout")  # timeout=None
        server = config.servers["srv1"]
        job_run = JobRun(job=job)
        job_run.start()
        # Job keeps "running" until global timeout
        call_count = 0
        def mock_running(*a, **kw):
            nonlocal call_count
            call_count += 1
            return True  # always running
        with unittest.mock.patch("jetbackup_remote.orchestrator.is_job_running", side_effect=mock_running):
            with unittest.mock.patch.object(orch, "_abort_running_job"):
                with unittest.mock.patch("jetbackup_remote.orchestrator.send_timeout_alert", return_value=True):
                    orch._poll_completion_only(server, job, job_run)
        self.assertEqual(job_run.status, JobStatus.TIMEOUT)

    def test_write_state_creates_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self._make_config()
            config.orchestrator.state_file = os.path.join(tmpdir, "state.json")
            orch = Orchestrator(config)
            orch._write_state({"version": "0.3.0", "test": True})
            with open(config.orchestrator.state_file) as f:
                data = json.load(f)
            self.assertEqual(data["version"], "0.3.0")
            self.assertTrue(data["test"])

    def test_write_state_overwrites(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self._make_config()
            config.orchestrator.state_file = os.path.join(tmpdir, "state.json")
            orch = Orchestrator(config)
            orch._write_state({"old": True})
            orch._write_state({"new": True})
            with open(config.orchestrator.state_file) as f:
                data = json.load(f)
            self.assertNotIn("old", data)
            self.assertTrue(data["new"])

    def test_interruptible_sleep_exits_on_shutdown(self):
        config = self._make_config()
        orch = Orchestrator(config)
        orch._shutdown_requested = True
        start = time.time()
        orch._interruptible_sleep(300)
        self.assertLess(time.time() - start, 2)

    def test_interruptible_sleep_sleeps(self):
        config = self._make_config()
        orch = Orchestrator(config)
        start = time.time()
        orch._interruptible_sleep(1)  # 1 second
        elapsed = time.time() - start
        self.assertGreaterEqual(elapsed, 0.9)

    def test_run_forever_executes_and_stops(self):
        config = self._make_config()
        orch = Orchestrator(config)
        cycle_count = 0

        def mock_run(**kwargs):
            nonlocal cycle_count
            cycle_count += 1
            if cycle_count >= 2:
                orch._shutdown_requested = True
            result = RunResult()
            result.start()
            result.finish()
            return result

        with unittest.mock.patch.object(orch, "run", side_effect=mock_run):
            with unittest.mock.patch.object(orch, "_interruptible_sleep"):
                with unittest.mock.patch("jetbackup_remote.orchestrator.send_daemon_lifecycle"):
                    with unittest.mock.patch("jetbackup_remote.orchestrator.send_cycle_summary"):
                        orch.run_forever()

        self.assertEqual(cycle_count, 2)

    def test_run_forever_calculates_pause(self):
        config = self._make_config()
        config.loop.target_interval = 100
        config.loop.min_pause = 10
        orch = Orchestrator(config)
        sleep_values = []

        def capture_sleep(seconds):
            sleep_values.append(seconds)
            orch._shutdown_requested = True

        mock_result = RunResult()
        mock_result.start()
        mock_result.finish()  # ~instant

        with unittest.mock.patch.object(orch, "run", return_value=mock_result):
            with unittest.mock.patch.object(orch, "_interruptible_sleep", side_effect=capture_sleep):
                with unittest.mock.patch("jetbackup_remote.orchestrator.send_daemon_lifecycle"):
                    orch.run_forever()

        self.assertEqual(len(sleep_values), 1)
        self.assertGreaterEqual(sleep_values[0], 10)  # at least min_pause

    def test_build_run_state_consecutive_failures(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self._make_config()
            config.orchestrator.state_file = os.path.join(tmpdir, "state.json")
            orch = Orchestrator(config)
            orch._daemon_started_at = "2026-01-01T00:00:00Z"

            # Write a previous state with 2 consecutive failures
            prev_state = {
                "last_run": {
                    "servers": {
                        "srv1": {"consecutive_failures": 2}
                    }
                }
            }
            with open(config.orchestrator.state_file, "w") as f:
                json.dump(prev_state, f)

            # Create a result where srv1 has all jobs skipped (unreachable)
            result = RunResult()
            result.start()
            sr = ServerRun(server_name="srv1", destination_id="d1")
            jr = JobRun(job=Job(job_id="j1", server_name="srv1"))
            jr.skip("SSH unreachable")
            sr.job_runs.append(jr)
            result.add_server_run(sr)
            result.add_job_run(jr)
            result.finish()

            state = orch._build_run_state(result, "2026-01-02T00:00:00Z", True)
            self.assertEqual(state["last_run"]["servers"]["srv1"]["consecutive_failures"], 3)

    def test_build_run_state_resets_on_success(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self._make_config()
            config.orchestrator.state_file = os.path.join(tmpdir, "state.json")
            orch = Orchestrator(config)
            orch._daemon_started_at = "2026-01-01T00:00:00Z"

            # Previous failures
            prev_state = {
                "last_run": {
                    "servers": {
                        "srv1": {"consecutive_failures": 5}
                    }
                }
            }
            with open(config.orchestrator.state_file, "w") as f:
                json.dump(prev_state, f)

            result = RunResult()
            result.start()
            sr = ServerRun(server_name="srv1", destination_id="d1")
            jr = JobRun(job=Job(job_id="j1", server_name="srv1"))
            jr.complete()
            sr.job_runs.append(jr)
            result.add_server_run(sr)
            result.add_job_run(jr)
            result.finish()

            state = orch._build_run_state(result, "2026-01-02T00:00:00Z", True)
            self.assertEqual(state["last_run"]["servers"]["srv1"]["consecutive_failures"], 0)

    def test_build_run_state_notification_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = self._make_config()
            config.orchestrator.state_file = os.path.join(tmpdir, "state.json")
            orch = Orchestrator(config)
            orch._daemon_started_at = "2026-01-01T00:00:00Z"

            result = RunResult()
            result.start()
            result.finish()

            state = orch._build_run_state(result, "next", notification_ok=False)
            self.assertEqual(state["last_notification_error"], "SMTP delivery failed")

    def tearDown(self):
        # Clean up state file if created in /tmp
        import glob
        for f in glob.glob("/tmp/test-jetbackup-state*.json"):
            try:
                os.unlink(f)
            except OSError:
                pass


if __name__ == "__main__":
    unittest.main()
