"""Tests for jetbackup_remote.config."""

import json
import os
import tempfile
import unittest

from jetbackup_remote.config import (
    Config, ConfigError, DestinationConfig, NotificationConfig, OrchestratorConfig,
    load_config, validate_config,
)
from jetbackup_remote.models import JobType


def _write_config(data: dict) -> str:
    """Write config dict to a temp file and return its path."""
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    return path


MINIMAL_CONFIG = {
    "servers": {
        "srv1": {"host": "srv1.example.com"}
    },
    "jobs": [
        {"job_id": "abc123", "server": "srv1", "label": "Test Backup"}
    ]
}

FULL_CONFIG = {
    "ssh_key": "/root/.ssh/id_ed25519",
    "servers": {
        "srv1": {"host": "srv1.example.com", "port": 51514, "user": "root"},
        "srv2": {"host": "srv2.example.com", "port": 22, "user": "backup"},
    },
    "jobs": [
        {"job_id": "abc", "server": "srv1", "label": "Accounts", "type": "accounts", "priority": 1},
        {"job_id": "def", "server": "srv1", "label": "Dirs", "type": "directories"},
        {"job_id": "ghi", "server": "srv2", "label": "DB", "type": "database"},
    ],
    "orchestrator": {
        "poll_interval": 60,
        "job_timeout": 7200,
        "lock_file": "/tmp/test.lock",
    },
    "notification": {
        "enabled": True,
        "smtp_host": "mail.example.com",
        "smtp_port": 587,
        "smtp_tls": True,
        "smtp_user": "user@example.com",
        "smtp_password": "secret",
        "from_address": "backup@example.com",
        "to_addresses": ["admin@example.com"],
    },
}


class TestLoadConfig(unittest.TestCase):

    def test_minimal_config(self):
        path = _write_config(MINIMAL_CONFIG)
        try:
            cfg = load_config(path)
            self.assertIn("srv1", cfg.servers)
            self.assertEqual(len(cfg.jobs), 1)
            self.assertEqual(cfg.jobs[0].job_id, "abc123")
            self.assertEqual(cfg.servers["srv1"].port, 51514)
        finally:
            os.unlink(path)

    def test_full_config(self):
        path = _write_config(FULL_CONFIG)
        try:
            cfg = load_config(path)
            self.assertEqual(len(cfg.servers), 2)
            self.assertEqual(len(cfg.jobs), 3)
            self.assertEqual(cfg.ssh_key, "/root/.ssh/id_ed25519")
            self.assertEqual(cfg.orchestrator.poll_interval, 60)
            self.assertEqual(cfg.orchestrator.job_timeout, 7200)
            self.assertTrue(cfg.notification.enabled)
            self.assertEqual(cfg.notification.smtp_port, 587)
        finally:
            os.unlink(path)

    def test_job_types_parsed(self):
        path = _write_config(FULL_CONFIG)
        try:
            cfg = load_config(path)
            types = {j.job_id: j.job_type for j in cfg.jobs}
            self.assertEqual(types["abc"], JobType.ACCOUNTS)
            self.assertEqual(types["def"], JobType.DIRECTORIES)
            self.assertEqual(types["ghi"], JobType.DATABASE)
        finally:
            os.unlink(path)

    def test_job_queue_ordering(self):
        path = _write_config(FULL_CONFIG)
        try:
            cfg = load_config(path)
            queue = cfg.job_queue
            # "abc" has priority=1, should come first
            self.assertEqual(queue[0].job_id, "abc")
        finally:
            os.unlink(path)

    def test_server_defaults(self):
        path = _write_config(MINIMAL_CONFIG)
        try:
            cfg = load_config(path)
            srv = cfg.servers["srv1"]
            self.assertEqual(srv.port, 51514)
            self.assertEqual(srv.user, "root")
            self.assertEqual(srv.ssh_timeout, 30)
        finally:
            os.unlink(path)

    def test_missing_file(self):
        with self.assertRaises(ConfigError) as ctx:
            load_config("/nonexistent/config.json")
        self.assertIn("not found", str(ctx.exception))

    def test_invalid_json(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            f.write("{invalid json}")
        try:
            with self.assertRaises(ConfigError) as ctx:
                load_config(path)
            self.assertIn("Invalid JSON", str(ctx.exception))
        finally:
            os.unlink(path)

    def test_no_servers(self):
        path = _write_config({"servers": {}, "jobs": [{"job_id": "a", "server": "x"}]})
        try:
            with self.assertRaises(ConfigError) as ctx:
                load_config(path)
            self.assertIn("at least one server", str(ctx.exception))
        finally:
            os.unlink(path)

    def test_no_jobs(self):
        path = _write_config({"servers": {"s": {"host": "h"}}, "jobs": []})
        try:
            with self.assertRaises(ConfigError) as ctx:
                load_config(path)
            self.assertIn("at least one job", str(ctx.exception))
        finally:
            os.unlink(path)

    def test_job_references_unknown_server(self):
        data = {
            "servers": {"srv1": {"host": "h"}},
            "jobs": [{"job_id": "a", "server": "nonexistent"}],
        }
        path = _write_config(data)
        try:
            with self.assertRaises(ConfigError) as ctx:
                load_config(path)
            self.assertIn("unknown server", str(ctx.exception))
        finally:
            os.unlink(path)

    def test_server_missing_host(self):
        data = {
            "servers": {"srv1": {"port": 22}},
            "jobs": [{"job_id": "a", "server": "srv1"}],
        }
        path = _write_config(data)
        try:
            with self.assertRaises(ConfigError) as ctx:
                load_config(path)
            self.assertIn("missing required field 'host'", str(ctx.exception))
        finally:
            os.unlink(path)

    def test_job_missing_server_field(self):
        data = {
            "servers": {"srv1": {"host": "h"}},
            "jobs": [{"job_id": "a"}],
        }
        path = _write_config(data)
        try:
            with self.assertRaises(ConfigError) as ctx:
                load_config(path)
            self.assertIn("missing required field 'server'", str(ctx.exception))
        finally:
            os.unlink(path)

    def test_unknown_job_type_defaults_to_other(self):
        data = {
            "servers": {"srv1": {"host": "h"}},
            "jobs": [{"job_id": "a", "server": "srv1", "type": "unknown_type"}],
        }
        path = _write_config(data)
        try:
            cfg = load_config(path)
            self.assertEqual(cfg.jobs[0].job_type, JobType.OTHER)
        finally:
            os.unlink(path)


class TestValidateConfig(unittest.TestCase):

    def test_valid_config_no_warnings(self):
        data = {
            "servers": {
                "srv1": {"host": "srv1.example.com", "destination_id": "dest_abc123"}
            },
            "jobs": [
                {"job_id": "abc123", "server": "srv1", "label": "Test Backup"}
            ]
        }
        path = _write_config(data)
        try:
            cfg = load_config(path)
            warnings = validate_config(cfg)
            self.assertEqual(len(warnings), 0)
        finally:
            os.unlink(path)

    def test_notifications_enabled_no_recipients(self):
        data = dict(MINIMAL_CONFIG)
        data["notification"] = {"enabled": True, "to_addresses": []}
        path = _write_config(data)
        try:
            cfg = load_config(path)
            warnings = validate_config(cfg)
            self.assertTrue(any("to_addresses" in w for w in warnings))
        finally:
            os.unlink(path)

    def test_duplicate_job_ids(self):
        data = {
            "servers": {"srv1": {"host": "h"}},
            "jobs": [
                {"job_id": "dup", "server": "srv1"},
                {"job_id": "dup", "server": "srv1"},
            ],
        }
        path = _write_config(data)
        try:
            cfg = load_config(path)
            warnings = validate_config(cfg)
            self.assertTrue(any("Duplicate" in w for w in warnings))
        finally:
            os.unlink(path)


class TestOrchestratorConfigDefaults(unittest.TestCase):

    def test_defaults(self):
        cfg = OrchestratorConfig()
        self.assertEqual(cfg.poll_interval, 30)
        self.assertEqual(cfg.job_timeout, 14400)
        self.assertEqual(cfg.startup_timeout, 120)
        self.assertEqual(cfg.lock_file, "/tmp/jetbackup-remote.lock")

    def test_notification_defaults(self):
        cfg = NotificationConfig()
        self.assertFalse(cfg.enabled)
        self.assertTrue(cfg.on_failure)
        self.assertTrue(cfg.on_timeout)
        self.assertFalse(cfg.on_complete)


class TestDestinationConfig(unittest.TestCase):

    def test_defaults(self):
        cfg = DestinationConfig()
        self.assertFalse(cfg.force_activate)
        self.assertTrue(cfg.skip_if_disabled)
        self.assertEqual(cfg.alert_days_without_jobs, 3)

    def test_destination_id_parsed(self):
        data = {
            "servers": {
                "srv1": {"host": "h", "destination_id": "dest_abc123"}
            },
            "jobs": [{"job_id": "a", "server": "srv1"}],
        }
        path = _write_config(data)
        try:
            cfg = load_config(path)
            self.assertEqual(cfg.servers["srv1"].destination_id, "dest_abc123")
        finally:
            os.unlink(path)

    def test_destination_id_optional(self):
        path = _write_config(MINIMAL_CONFIG)
        try:
            cfg = load_config(path)
            self.assertIsNone(cfg.servers["srv1"].destination_id)
        finally:
            os.unlink(path)

    def test_destination_config_parsed(self):
        data = dict(MINIMAL_CONFIG)
        data["destination"] = {
            "force_activate": True,
            "skip_if_disabled": False,
            "alert_days_without_jobs": 7,
        }
        path = _write_config(data)
        try:
            cfg = load_config(path)
            self.assertTrue(cfg.destination.force_activate)
            self.assertFalse(cfg.destination.skip_if_disabled)
            self.assertEqual(cfg.destination.alert_days_without_jobs, 7)
        finally:
            os.unlink(path)

    def test_destination_config_defaults(self):
        path = _write_config(MINIMAL_CONFIG)
        try:
            cfg = load_config(path)
            self.assertFalse(cfg.destination.force_activate)
            self.assertTrue(cfg.destination.skip_if_disabled)
        finally:
            os.unlink(path)

    def test_on_partial_default(self):
        cfg = NotificationConfig()
        self.assertTrue(cfg.on_partial)

    def test_warn_missing_destination_id(self):
        path = _write_config(MINIMAL_CONFIG)
        try:
            cfg = load_config(path)
            warnings = validate_config(cfg)
            self.assertTrue(
                any("destination_id" in w for w in warnings),
                f"Expected destination_id warning, got: {warnings}",
            )
        finally:
            os.unlink(path)

    def test_warn_duplicate_destination_id(self):
        data = {
            "servers": {
                "srv1": {"host": "h1", "destination_id": "dest1"},
                "srv2": {"host": "h2", "destination_id": "dest1"},
            },
            "jobs": [
                {"job_id": "a", "server": "srv1"},
                {"job_id": "b", "server": "srv2"},
            ],
        }
        path = _write_config(data)
        try:
            cfg = load_config(path)
            warnings = validate_config(cfg)
            self.assertTrue(
                any("Duplicate destination_id" in w for w in warnings),
                f"Expected duplicate warning, got: {warnings}",
            )
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
