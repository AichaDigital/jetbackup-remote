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
    set_job_enabled,
    get_destination, is_destination_disabled, set_destination_state,
    get_queue_group, list_queue_items, clear_queue,
    list_queue_groups, stop_queue_group, test_connection,
    list_logs, get_log, list_log_items, get_log_item,
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
        data["data"]["running"] = "True"
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


class TestSetJobEnabled(unittest.TestCase):

    @patch("jetbackup_remote.jetbackup_api.ssh_execute")
    def test_enable(self, mock_exec):
        mock_exec.return_value = SSHResult(stdout="OK", stderr="", returncode=0)
        result = set_job_enabled(_make_server(), "abc", enabled=True)
        self.assertTrue(result)
        cmd = mock_exec.call_args[0][1]
        self.assertIn("editBackupJob", cmd)
        self.assertIn("disabled=0", cmd)

    @patch("jetbackup_remote.jetbackup_api.ssh_execute")
    def test_disable(self, mock_exec):
        mock_exec.return_value = SSHResult(stdout="OK", stderr="", returncode=0)
        result = set_job_enabled(_make_server(), "abc", enabled=False)
        self.assertTrue(result)
        cmd = mock_exec.call_args[0][1]
        self.assertIn("disabled=1", cmd)

    @patch("jetbackup_remote.jetbackup_api.ssh_execute")
    def test_failure(self, mock_exec):
        mock_exec.return_value = SSHResult(stdout="", stderr="error", returncode=1)
        with self.assertRaises(JetBackupAPIError):
            set_job_enabled(_make_server(), "abc", enabled=True)


class TestGetDestination(unittest.TestCase):

    @patch("jetbackup_remote.jetbackup_api.ssh_execute")
    def test_enabled(self, mock_exec):
        mock_exec.return_value = SSHResult(
            stdout=_load_fixture("get_destination_enabled.json"),
            stderr="", returncode=0,
        )
        data = get_destination(_make_server(), "dest1")
        self.assertFalse(data["disabled"])
        self.assertEqual(data["name"], "RASP NAS")

    @patch("jetbackup_remote.jetbackup_api.ssh_execute")
    def test_disabled(self, mock_exec):
        mock_exec.return_value = SSHResult(
            stdout=_load_fixture("get_destination_disabled.json"),
            stderr="", returncode=0,
        )
        data = get_destination(_make_server(), "dest1")
        self.assertTrue(data["disabled"])

    @patch("jetbackup_remote.jetbackup_api.ssh_execute")
    def test_ssh_failure(self, mock_exec):
        mock_exec.side_effect = SSHError("connection refused")
        with self.assertRaises(JetBackupAPIError):
            get_destination(_make_server(), "dest1")


class TestIsDestinationDisabled(unittest.TestCase):

    @patch("jetbackup_remote.jetbackup_api.ssh_execute")
    def test_not_disabled(self, mock_exec):
        mock_exec.return_value = SSHResult(
            stdout=_load_fixture("get_destination_enabled.json"),
            stderr="", returncode=0,
        )
        self.assertFalse(is_destination_disabled(_make_server(), "dest1"))

    @patch("jetbackup_remote.jetbackup_api.ssh_execute")
    def test_disabled(self, mock_exec):
        mock_exec.return_value = SSHResult(
            stdout=_load_fixture("get_destination_disabled.json"),
            stderr="", returncode=0,
        )
        self.assertTrue(is_destination_disabled(_make_server(), "dest1"))

    @patch("jetbackup_remote.jetbackup_api.ssh_execute")
    def test_disabled_string(self, mock_exec):
        data = json.loads(_load_fixture("get_destination_enabled.json"))
        data["data"]["disabled"] = "True"
        mock_exec.return_value = SSHResult(
            stdout=json.dumps(data), stderr="", returncode=0,
        )
        self.assertTrue(is_destination_disabled(_make_server(), "dest1"))


class TestSetDestinationState(unittest.TestCase):

    @patch("jetbackup_remote.jetbackup_api.ssh_execute")
    def test_disable(self, mock_exec):
        mock_exec.return_value = SSHResult(stdout="OK", stderr="", returncode=0)
        result = set_destination_state(_make_server(), "dest1", disabled=True)
        self.assertTrue(result)
        cmd = mock_exec.call_args[0][1]
        self.assertIn("manageDestinationState", cmd)
        self.assertIn("disabled=1", cmd)

    @patch("jetbackup_remote.jetbackup_api.ssh_execute")
    def test_enable(self, mock_exec):
        mock_exec.return_value = SSHResult(stdout="OK", stderr="", returncode=0)
        result = set_destination_state(_make_server(), "dest1", disabled=False)
        self.assertTrue(result)
        cmd = mock_exec.call_args[0][1]
        self.assertIn("disabled=0", cmd)

    @patch("jetbackup_remote.jetbackup_api.ssh_execute")
    def test_failure(self, mock_exec):
        mock_exec.side_effect = SSHError("timeout")
        with self.assertRaises(JetBackupAPIError):
            set_destination_state(_make_server(), "dest1", disabled=True)


class TestGetQueueGroup(unittest.TestCase):

    @patch("jetbackup_remote.jetbackup_api.ssh_execute")
    def test_completed(self, mock_exec):
        mock_exec.return_value = SSHResult(
            stdout=_load_fixture("get_queue_group_completed.json"),
            stderr="", returncode=0,
        )
        data = get_queue_group(_make_server(), "qg_completed")
        self.assertEqual(data["status"], 100)
        self.assertEqual(data["progress"], 100)

    @patch("jetbackup_remote.jetbackup_api.ssh_execute")
    def test_partial_with_logs(self, mock_exec):
        mock_exec.return_value = SSHResult(
            stdout=_load_fixture("get_queue_group_partial.json"),
            stderr="", returncode=0,
        )
        data = get_queue_group(_make_server(), "qg_partial", get_log_contents=True)
        self.assertEqual(data["status"], 102)
        self.assertIn("Partial backup", data["log_contents"])
        cmd = mock_exec.call_args[0][1]
        self.assertIn("get_log_contents=1", cmd)

    @patch("jetbackup_remote.jetbackup_api.ssh_execute")
    def test_failed(self, mock_exec):
        mock_exec.return_value = SSHResult(
            stdout=_load_fixture("get_queue_group_failed.json"),
            stderr="", returncode=0,
        )
        data = get_queue_group(_make_server(), "qg_failed")
        self.assertEqual(data["status"], 200)

    @patch("jetbackup_remote.jetbackup_api.ssh_execute")
    def test_running(self, mock_exec):
        mock_exec.return_value = SSHResult(
            stdout=_load_fixture("get_queue_group_running.json"),
            stderr="", returncode=0,
        )
        data = get_queue_group(_make_server(), "qg_running")
        self.assertEqual(data["status"], 2)


class TestListQueueItems(unittest.TestCase):

    @patch("jetbackup_remote.jetbackup_api.ssh_execute")
    def test_list(self, mock_exec):
        mock_exec.return_value = SSHResult(
            stdout=_load_fixture("list_queue_items.json"),
            stderr="", returncode=0,
        )
        items = list_queue_items(_make_server(), "qg_completed")
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["_id"], "qi1")

    @patch("jetbackup_remote.jetbackup_api.ssh_execute")
    def test_failure(self, mock_exec):
        mock_exec.return_value = SSHResult(stdout="", stderr="error", returncode=1)
        with self.assertRaises(JetBackupAPIError):
            list_queue_items(_make_server(), "qg123")


class TestClearQueue(unittest.TestCase):

    @patch("jetbackup_remote.jetbackup_api.ssh_execute")
    def test_success(self, mock_exec):
        mock_exec.return_value = SSHResult(stdout="OK", stderr="", returncode=0)
        self.assertTrue(clear_queue(_make_server(), "qg123"))
        cmd = mock_exec.call_args[0][1]
        self.assertIn("clearQueue", cmd)


class TestListLogs(unittest.TestCase):

    @patch("jetbackup_remote.jetbackup_api.ssh_execute")
    def test_list(self, mock_exec):
        mock_exec.return_value = SSHResult(
            stdout=_load_fixture("list_logs.json"),
            stderr="", returncode=0,
        )
        logs = list_logs(_make_server())
        self.assertEqual(len(logs), 2)
        self.assertEqual(logs[0]["_id"], "log1")

    @patch("jetbackup_remote.jetbackup_api.ssh_execute")
    def test_with_type_filter(self, mock_exec):
        mock_exec.return_value = SSHResult(
            stdout=_load_fixture("list_logs.json"),
            stderr="", returncode=0,
        )
        list_logs(_make_server(), log_type="1")
        cmd = mock_exec.call_args[0][1]
        self.assertIn("type=1", cmd)


class TestGetLog(unittest.TestCase):

    @patch("jetbackup_remote.jetbackup_api.ssh_execute")
    def test_get(self, mock_exec):
        mock_exec.return_value = SSHResult(
            stdout=_load_fixture("get_log.json"),
            stderr="", returncode=0,
        )
        data = get_log(_make_server(), "log1")
        self.assertEqual(data["_id"], "log1")
        self.assertEqual(data["items_total"], 5)


class TestListLogItems(unittest.TestCase):

    @patch("jetbackup_remote.jetbackup_api.ssh_execute")
    def test_list(self, mock_exec):
        mock_exec.return_value = SSHResult(
            stdout=_load_fixture("list_log_items.json"),
            stderr="", returncode=0,
        )
        items = list_log_items(_make_server(), "log1")
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["account"], "user1")


class TestGetLogItem(unittest.TestCase):

    @patch("jetbackup_remote.jetbackup_api.ssh_execute")
    def test_get(self, mock_exec):
        mock_exec.return_value = SSHResult(
            stdout=_load_fixture("get_log_item.json"),
            stderr="", returncode=0,
        )
        data = get_log_item(_make_server(), "li1")
        self.assertEqual(data["_id"], "li1")
        self.assertEqual(data["account"], "user1")
        self.assertEqual(data["size"], 1073741824)


if __name__ == "__main__":
    unittest.main()
