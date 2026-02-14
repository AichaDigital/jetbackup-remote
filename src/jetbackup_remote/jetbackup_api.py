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


def _api_call(
    server: Server,
    function: str,
    params: Optional[dict] = None,
    ssh_key: Optional[str] = None,
    expect_data: bool = True,
) -> dict:
    """Execute a JetBackup5 API call via SSH.

    Encapsulates: build command → ssh_execute → check success → json.loads → unwrap envelope.

    Args:
        server: Target server.
        function: JetBackup API function name (e.g., 'getBackupJob').
        params: Dict of -D parameters (key=value).
        ssh_key: SSH key override.
        expect_data: If True, unwrap the 'data' key from response.

    Returns:
        Parsed response data (dict or list).

    Raises:
        JetBackupAPIError: If the API call fails at any level.
    """
    # Build command
    parts = [JETBACKUP_CMD, "-F", function]
    if params:
        for key, value in params.items():
            parts.append(f'-D "{key}={value}"')
    parts.append("-O json")
    cmd = " ".join(parts)

    # Execute via SSH
    try:
        result = ssh_execute(server, cmd, ssh_key=ssh_key)
    except SSHError as e:
        raise JetBackupAPIError(f"SSH failed for {function} on {server.name}: {e}")

    if not result.success:
        raise JetBackupAPIError(
            f"{function} failed on {server.name} (rc={result.returncode}): {result.stderr}"
        )

    # Parse JSON
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise JetBackupAPIError(
            f"Invalid JSON from {function} on {server.name}: {result.stdout[:200]}"
        )

    # Unwrap envelope: {"success":1, "data":{...}, "system":{...}}
    if expect_data and isinstance(data, dict) and "data" in data:
        return data["data"]
    return data


def _api_call_no_json(
    server: Server,
    function: str,
    params: Optional[dict] = None,
    ssh_key: Optional[str] = None,
) -> bool:
    """Execute a JetBackup5 API call that returns no JSON (action commands).

    Args:
        server: Target server.
        function: JetBackup API function name.
        params: Dict of -D parameters.
        ssh_key: SSH key override.

    Returns:
        True if command succeeded (rc=0).

    Raises:
        JetBackupAPIError: If the call fails.
    """
    parts = [JETBACKUP_CMD, "-F", function]
    if params:
        for key, value in params.items():
            parts.append(f'-D "{key}={value}"')
    cmd = " ".join(parts)

    try:
        result = ssh_execute(server, cmd, ssh_key=ssh_key)
    except SSHError as e:
        raise JetBackupAPIError(f"SSH failed for {function} on {server.name}: {e}")

    if not result.success:
        raise JetBackupAPIError(
            f"{function} failed on {server.name} (rc={result.returncode}): {result.stderr}"
        )

    return True


# --- Backup Job functions ---


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
    return _api_call(server, "getBackupJob", {"_id": job_id}, ssh_key=ssh_key)


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
    result = _api_call_no_json(server, "runBackupJobManually", {"_id": job_id}, ssh_key=ssh_key)
    logger.info("Triggered job %s on %s", job_id, server.name)
    return result


def set_job_enabled(
    server: Server,
    job_id: str,
    enabled: bool,
    ssh_key: Optional[str] = None,
) -> bool:
    """Enable or disable a backup job.

    Args:
        server: Target server.
        job_id: The backup job ID.
        enabled: True to enable, False to disable.
        ssh_key: SSH key override.

    Returns:
        True if the operation succeeded.

    Raises:
        JetBackupAPIError: If the API call fails.
    """
    value = "1" if enabled else "0"
    result = _api_call_no_json(
        server, "editBackupJob", {"_id": job_id, "enabled": value}, ssh_key=ssh_key,
    )
    action = "Enabled" if enabled else "Disabled"
    logger.info("%s job %s on %s", action, job_id, server.name)
    return result


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
    data = _api_call(server, "listBackupJobs", ssh_key=ssh_key, expect_data=False)

    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "data" in data:
        return data["data"]
    return [data]


# --- Destination functions ---


def get_destination(
    server: Server,
    dest_id: str,
    ssh_key: Optional[str] = None,
) -> dict:
    """Get destination details.

    Args:
        server: Target server.
        dest_id: Destination ID.
        ssh_key: SSH key override.

    Returns:
        Dict with destination data including 'disabled' field.

    Raises:
        JetBackupAPIError: If the API call fails.
    """
    return _api_call(server, "getDestination", {"_id": dest_id}, ssh_key=ssh_key)


def is_destination_disabled(
    server: Server,
    dest_id: str,
    ssh_key: Optional[str] = None,
) -> bool:
    """Check if a destination is disabled.

    Args:
        server: Target server.
        dest_id: Destination ID.
        ssh_key: SSH key override.

    Returns:
        True if the destination is disabled.
    """
    data = get_destination(server, dest_id, ssh_key)
    disabled = data.get("disabled", False)
    if isinstance(disabled, str):
        return disabled.lower() in ("true", "1", "yes")
    return bool(disabled)


def set_destination_state(
    server: Server,
    dest_id: str,
    disabled: bool,
    ssh_key: Optional[str] = None,
) -> bool:
    """Enable or disable a destination.

    Args:
        server: Target server.
        dest_id: Destination ID.
        disabled: True to disable, False to enable.
        ssh_key: SSH key override.

    Returns:
        True if the operation succeeded.

    Raises:
        JetBackupAPIError: If the API call fails.
    """
    value = "1" if disabled else "0"
    result = _api_call_no_json(
        server, "manageDestinationState", {"_id": dest_id, "disabled": value},
        ssh_key=ssh_key,
    )
    action = "Disabled" if disabled else "Enabled"
    logger.info("%s destination %s on %s", action, dest_id, server.name)
    return result


# --- Queue functions ---


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
    data = _api_call(server, "listQueueGroups", {"type": "1"}, ssh_key=ssh_key, expect_data=False)

    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "data" in data:
        return data["data"]
    return [data]


def get_queue_group(
    server: Server,
    group_id: str,
    get_log_contents: bool = False,
    ssh_key: Optional[str] = None,
) -> dict:
    """Get queue group details with optional log contents.

    Args:
        server: Target server.
        group_id: Queue group ID.
        get_log_contents: Include log contents in response.
        ssh_key: SSH key override.

    Returns:
        Dict with queue group data including 'status'.

    Raises:
        JetBackupAPIError: If the API call fails.
    """
    params = {"_id": group_id}
    if get_log_contents:
        params["get_log_contents"] = "1"
    return _api_call(server, "getQueueGroup", params, ssh_key=ssh_key)


def list_queue_items(
    server: Server,
    group_id: str,
    ssh_key: Optional[str] = None,
) -> list:
    """List items within a queue group.

    Args:
        server: Target server.
        group_id: Queue group ID.
        ssh_key: SSH key override.

    Returns:
        List of queue item dicts.

    Raises:
        JetBackupAPIError: If the API call fails.
    """
    data = _api_call(
        server, "listQueueItems", {"group_id": group_id},
        ssh_key=ssh_key, expect_data=False,
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
    result = _api_call_no_json(server, "stopQueueGroup", {"_id": group_id}, ssh_key=ssh_key)
    logger.info("Stopped queue group %s on %s", group_id, server.name)
    return result


def clear_queue(
    server: Server,
    group_id: str,
    ssh_key: Optional[str] = None,
) -> bool:
    """Clear a finished queue group.

    Args:
        server: Target server.
        group_id: Queue group ID to clear.
        ssh_key: SSH key override.

    Returns:
        True if cleared successfully.
    """
    result = _api_call_no_json(server, "clearQueue", {"_id": group_id}, ssh_key=ssh_key)
    logger.info("Cleared queue group %s on %s", group_id, server.name)
    return result


# --- Log functions ---


def list_logs(
    server: Server,
    log_type: Optional[str] = None,
    limit: int = 10,
    ssh_key: Optional[str] = None,
) -> list:
    """List recent logs.

    Args:
        server: Target server.
        log_type: Filter by log type.
        limit: Maximum number of logs.
        ssh_key: SSH key override.

    Returns:
        List of log dicts.

    Raises:
        JetBackupAPIError: If the API call fails.
    """
    params = {"limit": str(limit)}
    if log_type:
        params["type"] = log_type
    data = _api_call(server, "listLogs", params, ssh_key=ssh_key, expect_data=False)

    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "data" in data:
        return data["data"]
    return [data]


def get_log(
    server: Server,
    log_id: str,
    ssh_key: Optional[str] = None,
) -> dict:
    """Get log details.

    Args:
        server: Target server.
        log_id: Log ID.
        ssh_key: SSH key override.

    Returns:
        Dict with log data.

    Raises:
        JetBackupAPIError: If the API call fails.
    """
    return _api_call(server, "getLog", {"_id": log_id}, ssh_key=ssh_key)


def list_log_items(
    server: Server,
    log_id: str,
    ssh_key: Optional[str] = None,
) -> list:
    """List items within a log.

    Args:
        server: Target server.
        log_id: Log ID.
        ssh_key: SSH key override.

    Returns:
        List of log item dicts.

    Raises:
        JetBackupAPIError: If the API call fails.
    """
    data = _api_call(
        server, "listLogItems", {"_id": log_id},
        ssh_key=ssh_key, expect_data=False,
    )

    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "data" in data:
        return data["data"]
    return [data]


def get_log_item(
    server: Server,
    item_id: str,
    ssh_key: Optional[str] = None,
) -> dict:
    """Get log item details.

    Args:
        server: Target server.
        item_id: Log item ID.
        ssh_key: SSH key override.

    Returns:
        Dict with log item data.

    Raises:
        JetBackupAPIError: If the API call fails.
    """
    return _api_call(server, "getLogItem", {"_id": item_id}, ssh_key=ssh_key)


# --- Connectivity test ---


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
