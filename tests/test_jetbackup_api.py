"""Tests for jetbackup_remote.jetbackup_api."""

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from jetbackup_remote.models import Server
from jetbackup_remote.ssh import SSHError, SSHResult
from jetbackup_remote.jetbackup_api import (
    JetBackupAPIError,
    get_job_status, is_job_running, run_job, list_jobs,
    list_queue_groups, stop_queue_group, test_connection,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _load_fixture(name: str) -> str:
    """Load a fixture file as string."""
    return (FIXTURES / name).read_text()


def _make_server():
    return Server(name="srv1", host="srv1.example.com")


class TestGetJobStatus(unittest.TestCase):

    @patch("jetbackup_remote.jetbackup_api.ssh_execute")
    def test_idle_job(self, mock_exec):
        mock_exec.return_value = SSHResult(
            stdout=_load_fixture("get_backup_job_idle.json"),
            stderr="", returncode=0,
        )
        data = get_job_status(_make_server(), "aaaaaaaaaaaaaaaaaaaaaaaa")
        self.assertFalse(data["running"])
        self.assertEqual(data["name"], "Accounts Backup")

    @patch("jetbackup_remote.jetbackup_api.ssh_execute")
    def test_running_job(self, mock_exec):
        mock_exec.return_value = SSHResult(
            stdout=_load_fixture("get_backup_job_running.json"),
            stderr="", returncode=0,
        )
        data = get_job_status(_make_server(), "aaaaaaaaaaaaaaaaaaaaaaaa")
        self.assertTrue(data["running"])

    @patch("jetbackup_remote.jetbackup_api.ssh_execute")
    def test_ssh_failure(self, mock_exec):
        mock_exec.side_effect = SSHError("connection refused")
        with self.assertRaises(JetBackupAPIError):
            get_job_status(_make_server(), "abc")

    @patch("jetbackup_remote.jetbackup_api.ssh_execute")
    def test_nonzero_exit(self, mock_exec):
        mock_exec.return_value = SSHResult(stdout="", stderr="error", returncode=1)
        with self.assertRaises(JetBackupAPIError):
            get_job_status(_make_server(), "abc")

    @patch("jetbackup_remote.jetbackup_api.ssh_execute")
    def test_invalid_json(self, mock_exec):
        mock_exec.return_value = SSHResult(stdout="not json", stderr="", returncode=0)
        with self.assertRaises(JetBackupAPIError):
            get_job_status(_make_server(), "abc")


class TestIsJobRunning(unittest.TestCase):

    @patch("jetbackup_remote.jetbackup_api.ssh_execute")
    def test_not_running(self, mock_exec):
        mock_exec.return_value = SSHResult(
            stdout=_load_fixture("get_backup_job_idle.json"),
            stderr="", returncode=0,
        )
        self.assertFalse(is_job_running(_make_server(), "abc"))

    @patch("jetbackup_remote.jetbackup_api.ssh_execute")
    def test_running(self, mock_exec):
        mock_exec.return_value = SSHResult(
            stdout=_load_fixture("get_backup_job_running.json"),
            stderr="", returncode=0,
        )
        self.assertTrue(is_job_running(_make_server(), "abc"))

    @patch("jetbackup_remote.jetbackup_api.ssh_execute")
    def test_running_string_true(self, mock_exec):
        data = json.loads(_load_fixture("get_backup_job_idle.json"))
        data["running"] = "True"
        mock_exec.return_value = SSHResult(
            stdout=json.dumps(data), stderr="", returncode=0,
        )
        self.assertTrue(is_job_running(_make_server(), "abc"))


class TestRunJob(unittest.TestCase):

    @patch("jetbackup_remote.jetbackup_api.ssh_execute")
    def test_success(self, mock_exec):
        mock_exec.return_value = SSHResult(stdout="OK", stderr="", returncode=0)
        self.assertTrue(run_job(_make_server(), "abc"))

    @patch("jetbackup_remote.jetbackup_api.ssh_execute")
    def test_failure(self, mock_exec):
        mock_exec.return_value = SSHResult(stdout="", stderr="error", returncode=1)
        with self.assertRaises(JetBackupAPIError):
            run_job(_make_server(), "abc")


class TestListJobs(unittest.TestCase):

    @patch("jetbackup_remote.jetbackup_api.ssh_execute")
    def test_list(self, mock_exec):
        mock_exec.return_value = SSHResult(
            stdout=_load_fixture("list_backup_jobs.json"),
            stderr="", returncode=0,
        )
        jobs = list_jobs(_make_server())
        self.assertEqual(len(jobs), 2)
        self.assertEqual(jobs[0]["name"], "Accounts Backup")

    @patch("jetbackup_remote.jetbackup_api.ssh_execute")
    def test_list_with_data_wrapper(self, mock_exec):
        data = {"data": [{"_id": "a"}, {"_id": "b"}]}
        mock_exec.return_value = SSHResult(
            stdout=json.dumps(data), stderr="", returncode=0,
        )
        jobs = list_jobs(_make_server())
        self.assertEqual(len(jobs), 2)


class TestListQueueGroups(unittest.TestCase):

    @patch("jetbackup_remote.jetbackup_api.ssh_execute")
    def test_running_groups(self, mock_exec):
        mock_exec.return_value = SSHResult(
            stdout=_load_fixture("list_queue_groups_running.json"),
            stderr="", returncode=0,
        )
        groups = list_queue_groups(_make_server())
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["status"], 2)

    @patch("jetbackup_remote.jetbackup_api.ssh_execute")
    def test_empty_groups(self, mock_exec):
        mock_exec.return_value = SSHResult(
            stdout=_load_fixture("list_queue_groups_empty.json"),
            stderr="", returncode=0,
        )
        groups = list_queue_groups(_make_server())
        self.assertEqual(len(groups), 0)


class TestStopQueueGroup(unittest.TestCase):

    @patch("jetbackup_remote.jetbackup_api.ssh_execute")
    def test_success(self, mock_exec):
        mock_exec.return_value = SSHResult(stdout="OK", stderr="", returncode=0)
        self.assertTrue(stop_queue_group(_make_server(), "abc123"))


class TestTestConnection(unittest.TestCase):

    @patch("jetbackup_remote.jetbackup_api.ssh_execute")
    def test_all_ok(self, mock_exec):
        def side_effect(server, cmd, **kwargs):
            if "echo" in cmd:
                return SSHResult(stdout="jetbackup-remote-ok", stderr="", returncode=0)
            return SSHResult(
                stdout=_load_fixture("list_backup_jobs.json"),
                stderr="", returncode=0,
            )
        mock_exec.side_effect = side_effect

        result = test_connection(_make_server())
        self.assertTrue(result["ssh_ok"])
        self.assertTrue(result["jetbackup_ok"])
        self.assertIsNone(result["error"])

    @patch("jetbackup_remote.jetbackup_api.ssh_execute")
    def test_ssh_fails(self, mock_exec):
        mock_exec.side_effect = SSHError("connection refused")
        result = test_connection(_make_server())
        self.assertFalse(result["ssh_ok"])
        self.assertFalse(result["jetbackup_ok"])
        self.assertIn("SSH connection failed", result["error"])

    @patch("jetbackup_remote.jetbackup_api.ssh_execute")
    def test_jetbackup_fails(self, mock_exec):
        def side_effect(server, cmd, **kwargs):
            if "echo" in cmd:
                return SSHResult(stdout="jetbackup-remote-ok", stderr="", returncode=0)
            return SSHResult(stdout="", stderr="not found", returncode=127)
        mock_exec.side_effect = side_effect

        result = test_connection(_make_server())
        self.assertTrue(result["ssh_ok"])
        self.assertFalse(result["jetbackup_ok"])
        self.assertIn("JetBackup API failed", result["error"])


if __name__ == "__main__":
    unittest.main()
