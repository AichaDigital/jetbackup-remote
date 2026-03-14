"""Tests for CLI commands."""

import unittest
import sys

from jetbackup_remote.cli import build_parser


class TestCLIParser(unittest.TestCase):

    def test_daemon_subcommand_exists(self):
        parser = build_parser()
        args = parser.parse_args(["daemon"])
        self.assertEqual(args.command, "daemon")

    def test_daemon_accepts_config(self):
        parser = build_parser()
        args = parser.parse_args(["-c", "/tmp/test.json", "daemon"])
        self.assertEqual(args.config, "/tmp/test.json")

    def test_daemon_accepts_verbose(self):
        parser = build_parser()
        args = parser.parse_args(["-v", "daemon"])
        self.assertTrue(args.verbose)

    def test_daemon_no_dry_run(self):
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["daemon", "--dry-run"])

    def test_daemon_no_server_filter(self):
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["daemon", "--server", "srv1"])

    def test_run_subcommand_still_works(self):
        parser = build_parser()
        args = parser.parse_args(["run", "--dry-run"])
        self.assertEqual(args.command, "run")
        self.assertTrue(args.dry_run)


if __name__ == "__main__":
    unittest.main()
