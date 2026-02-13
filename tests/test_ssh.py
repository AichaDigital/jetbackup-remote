"""Tests for jetbackup_remote.ssh."""

import subprocess
import unittest
from unittest.mock import patch, MagicMock

from jetbackup_remote.models import Server
from jetbackup_remote.ssh import (
    SSHError, SSHResult, build_ssh_command, ssh_execute, ssh_test,
)


class TestBuildSSHCommand(unittest.TestCase):

    def test_basic_command(self):
        server = Server(name="srv1", host="srv1.example.com")
        cmd = build_ssh_command(server, "echo test")
        self.assertEqual(cmd[0], "ssh")
        self.assertIn("-o", cmd)
        self.assertIn("BatchMode=yes", cmd)
        self.assertIn("-p", cmd)
        self.assertIn("51514", cmd)
        self.assertIn("root@srv1.example.com", cmd)
        self.assertIn("echo test", cmd)

    def test_custom_port(self):
        server = Server(name="srv1", host="srv1.example.com", port=22)
        cmd = build_ssh_command(server, "echo test")
        idx = cmd.index("-p")
        self.assertEqual(cmd[idx + 1], "22")

    def test_ssh_key(self):
        server = Server(name="srv1", host="srv1.example.com")
        cmd = build_ssh_command(server, "echo test", ssh_key="/root/.ssh/id_ed25519")
        self.assertIn("-i", cmd)
        self.assertIn("/root/.ssh/id_ed25519", cmd)

    def test_server_ssh_key(self):
        server = Server(name="srv1", host="srv1.example.com", ssh_key="/path/key")
        cmd = build_ssh_command(server, "echo test")
        self.assertIn("-i", cmd)
        self.assertIn("/path/key", cmd)

    def test_ssh_key_override(self):
        server = Server(name="srv1", host="srv1.example.com", ssh_key="/path/key1")
        cmd = build_ssh_command(server, "echo test", ssh_key="/path/key2")
        self.assertIn("/path/key2", cmd)
        self.assertNotIn("/path/key1", cmd)

    def test_connect_timeout(self):
        server = Server(name="srv1", host="srv1.example.com", ssh_timeout=10)
        cmd = build_ssh_command(server, "echo test")
        self.assertIn("ConnectTimeout=10", cmd)

    def test_custom_user(self):
        server = Server(name="srv1", host="srv1.example.com", user="backup")
        cmd = build_ssh_command(server, "echo test")
        self.assertIn("backup@srv1.example.com", cmd)


class TestSSHResult(unittest.TestCase):

    def test_success(self):
        r = SSHResult(stdout="ok", stderr="", returncode=0)
        self.assertTrue(r.success)

    def test_failure(self):
        r = SSHResult(stdout="", stderr="error", returncode=1)
        self.assertFalse(r.success)


class TestSSHExecute(unittest.TestCase):

    @patch("jetbackup_remote.ssh.subprocess.run")
    def test_success(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="hello\n", stderr="", returncode=0
        )
        server = Server(name="srv1", host="srv1.example.com")
        result = ssh_execute(server, "echo hello")
        self.assertTrue(result.success)
        self.assertEqual(result.stdout, "hello")
        mock_run.assert_called_once()

    @patch("jetbackup_remote.ssh.subprocess.run")
    def test_connection_failure_rc255(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="", stderr="Connection refused", returncode=255
        )
        server = Server(name="srv1", host="srv1.example.com")
        with self.assertRaises(SSHError) as ctx:
            ssh_execute(server, "echo hello")
        self.assertEqual(ctx.exception.returncode, 255)
        self.assertIn("connection failed", str(ctx.exception).lower())

    @patch("jetbackup_remote.ssh.subprocess.run")
    def test_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="ssh", timeout=30)
        server = Server(name="srv1", host="srv1.example.com")
        with self.assertRaises(SSHError) as ctx:
            ssh_execute(server, "echo hello", timeout=30)
        self.assertIn("timed out", str(ctx.exception).lower())

    @patch("jetbackup_remote.ssh.subprocess.run")
    def test_remote_command_failure(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="", stderr="command not found", returncode=127
        )
        server = Server(name="srv1", host="srv1.example.com")
        result = ssh_execute(server, "nonexistent_command")
        self.assertFalse(result.success)
        self.assertEqual(result.returncode, 127)

    @patch("jetbackup_remote.ssh.subprocess.run")
    def test_default_timeout(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="ok", stderr="", returncode=0
        )
        server = Server(name="srv1", host="srv1.example.com", ssh_timeout=15)
        ssh_execute(server, "echo hello")
        call_kwargs = mock_run.call_args[1]
        self.assertEqual(call_kwargs["timeout"], 60)  # 15 * 4


class TestSSHTest(unittest.TestCase):

    @patch("jetbackup_remote.ssh.ssh_execute")
    def test_success(self, mock_exec):
        mock_exec.return_value = SSHResult(
            stdout="jetbackup-remote-ok", stderr="", returncode=0
        )
        server = Server(name="srv1", host="srv1.example.com")
        self.assertTrue(ssh_test(server))

    @patch("jetbackup_remote.ssh.ssh_execute")
    def test_failure_wrong_output(self, mock_exec):
        mock_exec.return_value = SSHResult(
            stdout="wrong", stderr="", returncode=0
        )
        server = Server(name="srv1", host="srv1.example.com")
        self.assertFalse(ssh_test(server))

    @patch("jetbackup_remote.ssh.ssh_execute")
    def test_failure_ssh_error(self, mock_exec):
        mock_exec.side_effect = SSHError("connection refused")
        server = Server(name="srv1", host="srv1.example.com")
        self.assertFalse(ssh_test(server))


if __name__ == "__main__":
    unittest.main()
