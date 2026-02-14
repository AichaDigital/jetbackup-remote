"""Command-line interface for jetbackup-remote."""

import argparse
import json
import logging
import sys
from typing import Optional

from . import __version__
from .config import ConfigError, load_config, validate_config
from .jetbackup_api import (
    JetBackupAPIError,
    get_destination,
    is_destination_disabled,
    test_connection,
    list_jobs as api_list_jobs,
    stop_queue_group,
)
from .logging_config import setup_logging
from .models import JobStatus
from .notifier import send_notification
from .orchestrator import LockError, Orchestrator

DEFAULT_CONFIG = "/etc/jetbackup-remote/config.json"

logger = logging.getLogger("jetbackup_remote.cli")


def _load_and_validate_config(args) -> Optional[object]:
    """Load config from args.config path."""
    try:
        config = load_config(args.config)
        return config
    except ConfigError as e:
        print(f"Error: {e}", file=sys.stderr)
        return None


def cmd_run(args) -> int:
    """Execute the orchestration run."""
    config = _load_and_validate_config(args)
    if config is None:
        return 1

    setup_logging(
        log_file=config.orchestrator.log_file,
        max_bytes=config.orchestrator.log_max_bytes,
        backup_count=config.orchestrator.log_backup_count,
        verbose=args.verbose,
    )

    orch = Orchestrator(config)

    if not args.dry_run:
        try:
            orch.acquire_lock()
        except LockError as e:
            logger.error(str(e))
            print(f"Error: {e}", file=sys.stderr)
            return 1

    try:
        orch.install_signal_handlers()

        # Determine force_activate from CLI flag
        force_activate = getattr(args, "force_activate_destination", False) or None

        result = orch.run(
            dry_run=args.dry_run,
            server_filter=getattr(args, "server", None),
            job_filter=getattr(args, "job", None),
            force_activate=force_activate,
        )

        # Send notification
        if not args.dry_run:
            send_notification(config.notification, result)

        # Output summary
        print(result.summary_line())
        if not result.success:
            for jr in result.jobs:
                if jr.status in (JobStatus.FAILED, JobStatus.TIMEOUT):
                    print(
                        f"  {jr.status.value}: {jr.job.display_name} "
                        f"on {jr.job.server_name}"
                        f"{' - ' + jr.error_message if jr.error_message else ''}",
                    )

        # Show deactivation errors prominently
        if result.has_deactivation_errors:
            print("\nCRITICAL: Destination deactivation failed!")
            for sr in result.server_runs:
                if sr.has_deactivation_error:
                    print(f"  {sr.server_name}: {sr.deactivation_error}")

        return 0 if result.success else 2

    finally:
        if not args.dry_run:
            orch.release_lock()


def cmd_status(args) -> int:
    """Show current job status on servers."""
    config = _load_and_validate_config(args)
    if config is None:
        return 1

    results = {}
    for name, server in config.servers.items():
        if args.server and name != args.server:
            continue

        conn = test_connection(server, ssh_key=config.ssh_key)
        server_jobs = [j for j in config.jobs if j.server_name == name]

        results[name] = {
            "ssh_ok": conn["ssh_ok"],
            "jetbackup_ok": conn["jetbackup_ok"],
            "error": conn.get("error"),
            "destination_id": server.destination_id,
            "destination_disabled": None,
            "jobs": [],
        }

        if conn["jetbackup_ok"]:
            # Check destination state
            if server.destination_id:
                try:
                    disabled = is_destination_disabled(
                        server, server.destination_id, ssh_key=config.ssh_key,
                    )
                    results[name]["destination_disabled"] = disabled
                except Exception:
                    results[name]["destination_disabled"] = None

            from .jetbackup_api import is_job_running
            for job in server_jobs:
                try:
                    running = is_job_running(server, job.job_id, ssh_key=config.ssh_key)
                    results[name]["jobs"].append({
                        "id": job.job_id,
                        "label": job.label,
                        "running": running,
                    })
                except Exception as e:
                    results[name]["jobs"].append({
                        "id": job.job_id,
                        "label": job.label,
                        "error": str(e),
                    })

    if getattr(args, "json", False):
        print(json.dumps(results, indent=2))
    else:
        for name, data in results.items():
            ssh_icon = "OK" if data["ssh_ok"] else "FAIL"
            jb_icon = "OK" if data["jetbackup_ok"] else "FAIL"
            dest_status = ""
            if data["destination_id"]:
                if data["destination_disabled"] is True:
                    dest_status = " DEST=DISABLED"
                elif data["destination_disabled"] is False:
                    dest_status = " DEST=ENABLED"
                else:
                    dest_status = " DEST=UNKNOWN"
            print(f"{name}: SSH={ssh_icon} JB5={jb_icon}{dest_status}")
            if data["error"]:
                print(f"  Error: {data['error']}")
            for job in data["jobs"]:
                if "error" in job:
                    print(f"  {job['label']} ({job['id']}): ERROR - {job['error']}")
                else:
                    state = "RUNNING" if job["running"] else "idle"
                    print(f"  {job['label']} ({job['id']}): {state}")

    return 0


def cmd_test(args) -> int:
    """Test SSH connectivity to servers."""
    config = _load_and_validate_config(args)
    if config is None:
        return 1

    all_ok = True
    for name, server in config.servers.items():
        if args.server and name != args.server:
            continue

        conn = test_connection(server, ssh_key=config.ssh_key)
        ssh_status = "OK" if conn["ssh_ok"] else "FAIL"
        jb_status = "OK" if conn["jetbackup_ok"] else "FAIL"

        print(f"{name} ({server.host}:{server.port}): SSH={ssh_status} JB5={jb_status}")
        if conn["error"]:
            print(f"  {conn['error']}")
            all_ok = False

    return 0 if all_ok else 1


def cmd_list(args) -> int:
    """List configured jobs."""
    config = _load_and_validate_config(args)
    if config is None:
        return 1

    jobs = config.job_queue

    if getattr(args, "json", False):
        data = [{
            "job_id": j.job_id,
            "server": j.server_name,
            "label": j.label,
            "type": j.job_type.value,
            "priority": j.priority,
        } for j in jobs]
        print(json.dumps(data, indent=2))
    else:
        print(f"{'#':<3} {'Server':<15} {'Label':<25} {'Type':<12} {'Job ID'}")
        print(f"{'-'*3} {'-'*15} {'-'*25} {'-'*12} {'-'*24}")
        for i, j in enumerate(jobs, 1):
            print(f"{i:<3} {j.server_name:<15} {j.label:<25} {j.job_type.value:<12} {j.job_id}")

    return 0


def cmd_stop(args) -> int:
    """Stop a running queue group on a server."""
    config = _load_and_validate_config(args)
    if config is None:
        return 1

    server = config.servers.get(args.server)
    if server is None:
        print(f"Error: Unknown server '{args.server}'", file=sys.stderr)
        return 1

    try:
        stop_queue_group(server, args.job_id, ssh_key=config.ssh_key)
        print(f"Stopped queue group {args.job_id} on {args.server}")
        return 0
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def cmd_validate(args) -> int:
    """Validate configuration file."""
    config = _load_and_validate_config(args)
    if config is None:
        return 1

    warnings = validate_config(config)

    print(f"Config: {args.config}")
    print(f"Servers: {len(config.servers)}")
    print(f"Jobs: {len(config.jobs)}")
    print(f"Notifications: {'enabled' if config.notification.enabled else 'disabled'}")
    print(f"Poll interval: {config.orchestrator.poll_interval}s")
    print(f"Job timeout: {config.orchestrator.job_timeout}s")
    print(f"Destination lifecycle: force_activate={config.destination.force_activate}, "
          f"skip_if_disabled={config.destination.skip_if_disabled}")

    if warnings:
        print(f"\nWarnings ({len(warnings)}):")
        for w in warnings:
            print(f"  - {w}")
        return 1

    print("\nConfig is valid.")
    return 0


def cmd_destinations(args) -> int:
    """Show destination status on all servers."""
    config = _load_and_validate_config(args)
    if config is None:
        return 1

    results = {}
    for name, server in config.servers.items():
        if args.server and name != args.server:
            continue

        if not server.destination_id:
            results[name] = {
                "destination_id": None,
                "status": "not configured",
            }
            continue

        conn = test_connection(server, ssh_key=config.ssh_key)
        if not conn["jetbackup_ok"]:
            results[name] = {
                "destination_id": server.destination_id,
                "status": "unreachable",
                "error": conn.get("error"),
            }
            continue

        try:
            dest_data = get_destination(
                server, server.destination_id, ssh_key=config.ssh_key,
            )
            disabled = dest_data.get("disabled", False)
            if isinstance(disabled, str):
                disabled = disabled.lower() in ("true", "1", "yes")

            results[name] = {
                "destination_id": server.destination_id,
                "status": "disabled" if disabled else "enabled",
                "name": dest_data.get("name", ""),
                "disk_usage": dest_data.get("disk_usage"),
            }
        except Exception as e:
            results[name] = {
                "destination_id": server.destination_id,
                "status": "error",
                "error": str(e),
            }

    if getattr(args, "json", False):
        print(json.dumps(results, indent=2))
    else:
        for name, data in results.items():
            if data["destination_id"] is None:
                print(f"{name}: no destination_id configured")
            else:
                status = data["status"].upper()
                dest_name = data.get("name", "")
                extra = f" ({dest_name})" if dest_name else ""
                print(f"{name}: {status}{extra} [id={data['destination_id']}]")
                if data.get("error"):
                    print(f"  Error: {data['error']}")
                if data.get("disk_usage") is not None:
                    print(f"  Disk usage: {data['disk_usage']}")

    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog="jetbackup-remote",
        description="Remote orchestrator for JetBackup5 backup serialization",
    )
    parser.add_argument(
        "--version", action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "-c", "--config",
        default=DEFAULT_CONFIG,
        help=f"Config file path (default: {DEFAULT_CONFIG})",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Verbose output (DEBUG level)",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # run
    p_run = subparsers.add_parser("run", help="Execute backup orchestration")
    p_run.add_argument("--dry-run", action="store_true", help="Simulate without triggering")
    p_run.add_argument("--server", help="Only run jobs for this server")
    p_run.add_argument("--job", help="Only run this specific job ID")
    p_run.add_argument(
        "--force-activate-destination",
        action="store_true",
        dest="force_activate_destination",
        help="Force activate destination even if disabled (overrides skip_if_disabled)",
    )

    # status
    p_status = subparsers.add_parser("status", help="Show job status on servers")
    p_status.add_argument("--server", help="Only show this server")
    p_status.add_argument("--json", action="store_true", help="JSON output")

    # test
    p_test = subparsers.add_parser("test", help="Test SSH connectivity")
    p_test.add_argument("--server", help="Only test this server")

    # list
    p_list = subparsers.add_parser("list", help="List configured jobs")
    p_list.add_argument("--json", action="store_true", help="JSON output")

    # stop
    p_stop = subparsers.add_parser("stop", help="Stop a running queue group")
    p_stop.add_argument("--server", required=True, help="Server name")
    p_stop.add_argument("job_id", help="Queue group ID to stop")

    # validate
    subparsers.add_parser("validate", help="Validate configuration")

    # destinations
    p_dest = subparsers.add_parser("destinations", help="Show destination status")
    p_dest.add_argument("--server", help="Only show this server")
    p_dest.add_argument("--json", action="store_true", help="JSON output")

    return parser


def main(argv=None) -> int:
    """Main entry point."""
    parser = build_parser()
    args = parser.parse_args(argv)

    commands = {
        "run": cmd_run,
        "status": cmd_status,
        "test": cmd_test,
        "list": cmd_list,
        "stop": cmd_stop,
        "validate": cmd_validate,
        "destinations": cmd_destinations,
    }

    handler = commands.get(args.command)
    if handler is None:
        parser.print_help()
        return 1

    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
