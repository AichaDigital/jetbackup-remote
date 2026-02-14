"""JetBackup5 API wrapper via SSH + jetbackup5api CLI."""

import json
import logging
from typing import Optional

from .models import Server, QueueGroupStatus
from .ssh import SSHError, ssh_execute

logger = logging.getLogger("jetbackup_remote.jetbackup_api")

JETBACKUP_CMD = "jetbackup5api"


class JetBackupAPIError(Exception):
    """Raised when a JetBackup API call fails."""
    pass


def get_job_status(
    server: Server,
    job_id: str,
    ssh_key: Optional[str] = None,
) -> dict:
    """Get the status of a backup job.

    Args:
        server: Target server with JetBackup5.
        job_id: The backup job ID.
        ssh_key: SSH key override.

    Returns:
        Dict with job data including 'running' field.

    Raises:
        JetBackupAPIError: If the API call fails.
    """
    cmd = f'{JETBACKUP_CMD} -F getBackupJob -D "_id={job_id}" -O json'

    try:
        result = ssh_execute(server, cmd, ssh_key=ssh_key)
    except SSHError as e:
        raise JetBackupAPIError(f"SSH failed for getBackupJob on {server.name}: {e}")

    if not result.success:
        raise JetBackupAPIError(
            f"getBackupJob failed on {server.name} (rc={result.returncode}): {result.stderr}"
        )

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise JetBackupAPIError(
            f"Invalid JSON from getBackupJob on {server.name}: {result.stdout[:200]}"
        )

    # JetBackup5 API wraps response: {"success":1, "data":{...}, "system":{...}}
    if isinstance(data, dict) and "data" in data:
        return data["data"]
    return data


def is_job_running(
    server: Server,
    job_id: str,
    ssh_key: Optional[str] = None,
) -> bool:
    """Check if a backup job is currently running.

    Args:
        server: Target server.
        job_id: The backup job ID.
        ssh_key: SSH key override.

    Returns:
        True if the job is running.
    """
    data = get_job_status(server, job_id, ssh_key)

    # JetBackup5 API returns 'running' as True/False
    running = data.get("running", False)
    if isinstance(running, str):
        return running.lower() in ("true", "1", "yes")
    return bool(running)


def run_job(
    server: Server,
    job_id: str,
    ssh_key: Optional[str] = None,
) -> bool:
    """Trigger a backup job to run.

    Args:
        server: Target server.
        job_id: The backup job ID.
        ssh_key: SSH key override.

    Returns:
        True if the job was triggered successfully.

    Raises:
        JetBackupAPIError: If the API call fails.
    """
    cmd = f'{JETBACKUP_CMD} -F runBackupJobManually -D "_id={job_id}"'

    try:
        result = ssh_execute(server, cmd, ssh_key=ssh_key)
    except SSHError as e:
        raise JetBackupAPIError(f"SSH failed for runBackupJobManually on {server.name}: {e}")

    if not result.success:
        raise JetBackupAPIError(
            f"runBackupJobManually failed on {server.name} (rc={result.returncode}): {result.stderr}"
        )

    logger.info("Triggered job %s on %s", job_id, server.name)
    return True


def list_jobs(
    server: Server,
    ssh_key: Optional[str] = None,
) -> list:
    """List all backup jobs on a server.

    Args:
        server: Target server.
        ssh_key: SSH key override.

    Returns:
        List of job dicts.

    Raises:
        JetBackupAPIError: If the API call fails.
    """
    cmd = f"{JETBACKUP_CMD} -F listBackupJobs -O json"

    try:
        result = ssh_execute(server, cmd, ssh_key=ssh_key)
    except SSHError as e:
        raise JetBackupAPIError(f"SSH failed for listBackupJobs on {server.name}: {e}")

    if not result.success:
        raise JetBackupAPIError(
            f"listBackupJobs failed on {server.name} (rc={result.returncode}): {result.stderr}"
        )

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise JetBackupAPIError(
            f"Invalid JSON from listBackupJobs on {server.name}: {result.stdout[:200]}"
        )

    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "data" in data:
        return data["data"]
    return [data]


def list_queue_groups(
    server: Server,
    ssh_key: Optional[str] = None,
) -> list:
    """List backup queue groups (running operations).

    Args:
        server: Target server.
        ssh_key: SSH key override.

    Returns:
        List of queue group dicts.

    Raises:
        JetBackupAPIError: If the API call fails.
    """
    cmd = f'{JETBACKUP_CMD} -F listQueueGroups -D "type=1" -O json'

    try:
        result = ssh_execute(server, cmd, ssh_key=ssh_key)
    except SSHError as e:
        raise JetBackupAPIError(f"SSH failed for listQueueGroups on {server.name}: {e}")

    if not result.success:
        raise JetBackupAPIError(
            f"listQueueGroups failed on {server.name} (rc={result.returncode}): {result.stderr}"
        )

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise JetBackupAPIError(
            f"Invalid JSON from listQueueGroups on {server.name}: {result.stdout[:200]}"
        )

    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "data" in data:
        return data["data"]
    return [data]


def stop_queue_group(
    server: Server,
    group_id: str,
    ssh_key: Optional[str] = None,
) -> bool:
    """Stop a running queue group.

    Args:
        server: Target server.
        group_id: Queue group ID to stop.
        ssh_key: SSH key override.

    Returns:
        True if stopped successfully.
    """
    cmd = f'{JETBACKUP_CMD} -F stopQueueGroup -D "_id={group_id}"'

    try:
        result = ssh_execute(server, cmd, ssh_key=ssh_key)
    except SSHError as e:
        raise JetBackupAPIError(f"SSH failed for stopQueueGroup on {server.name}: {e}")

    if not result.success:
        raise JetBackupAPIError(
            f"stopQueueGroup failed on {server.name} (rc={result.returncode}): {result.stderr}"
        )

    logger.info("Stopped queue group %s on %s", group_id, server.name)
    return True


def test_connection(
    server: Server,
    ssh_key: Optional[str] = None,
) -> dict:
    """Test SSH + JetBackup5 connectivity.

    Returns a dict with:
        - ssh_ok: bool
        - jetbackup_ok: bool
        - error: Optional error message
    """
    result = {"ssh_ok": False, "jetbackup_ok": False, "error": None}

    # Test SSH
    try:
        ssh_result = ssh_execute(
            server,
            'echo "jetbackup-remote-ok"',
            ssh_key=ssh_key,
            timeout=server.ssh_timeout,
        )
        if ssh_result.success and "jetbackup-remote-ok" in ssh_result.stdout:
            result["ssh_ok"] = True
        else:
            result["error"] = f"SSH echo failed: {ssh_result.stderr}"
            return result
    except SSHError as e:
        result["error"] = f"SSH connection failed: {e}"
        return result

    # Test JetBackup5 API
    try:
        ssh_result = ssh_execute(
            server,
            f"{JETBACKUP_CMD} -F listBackupJobs -O json",
            ssh_key=ssh_key,
        )
        if ssh_result.success:
            result["jetbackup_ok"] = True
        else:
            result["error"] = f"JetBackup API failed: {ssh_result.stderr}"
    except SSHError as e:
        result["error"] = f"JetBackup API SSH failed: {e}"

    return result
