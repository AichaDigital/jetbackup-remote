"""SSH command execution wrapper using subprocess."""

import logging
import subprocess
from dataclasses import dataclass
from typing import Optional

from .models import Server

logger = logging.getLogger("jetbackup_remote.ssh")


class SSHError(Exception):
    """Raised when an SSH command fails."""
    def __init__(self, message: str, returncode: int = -1, stderr: str = ""):
        super().__init__(message)
        self.returncode = returncode
        self.stderr = stderr


@dataclass
class SSHResult:
    """Result of an SSH command execution."""
    stdout: str
    stderr: str
    returncode: int

    @property
    def success(self) -> bool:
        return self.returncode == 0


def build_ssh_command(
    server: Server,
    remote_command: str,
    ssh_key: Optional[str] = None,
) -> list:
    """Build the SSH command list for subprocess.

    Args:
        server: Target server.
        remote_command: Command to execute remotely.
        ssh_key: Path to SSH private key (overrides server.ssh_key).

    Returns:
        List of command arguments for subprocess.
    """
    cmd = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", f"ConnectTimeout={server.ssh_timeout}",
        "-p", str(server.port),
    ]

    key = ssh_key or server.ssh_key
    if key:
        cmd.extend(["-i", key])

    cmd.append(server.ssh_target())
    cmd.append(remote_command)

    return cmd


def ssh_execute(
    server: Server,
    remote_command: str,
    ssh_key: Optional[str] = None,
    timeout: Optional[int] = None,
) -> SSHResult:
    """Execute a command on a remote server via SSH.

    Each call creates a new SSH connection (resilient to network issues).

    Args:
        server: Target server.
        remote_command: Command to execute.
        ssh_key: SSH key path override.
        timeout: Command timeout in seconds (None = server.ssh_timeout * 4).

    Returns:
        SSHResult with stdout, stderr, returncode.

    Raises:
        SSHError: On connection failure or timeout.
    """
    cmd = build_ssh_command(server, remote_command, ssh_key)
    effective_timeout = timeout or (server.ssh_timeout * 4)

    logger.debug("SSH %s: %s", server.name, remote_command)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=effective_timeout,
        )
        ssh_result = SSHResult(
            stdout=result.stdout.strip(),
            stderr=result.stderr.strip(),
            returncode=result.returncode,
        )

        if result.returncode == 255:
            raise SSHError(
                f"SSH connection failed to {server.name}: {result.stderr.strip()}",
                returncode=255,
                stderr=result.stderr.strip(),
            )

        logger.debug(
            "SSH %s: rc=%d stdout=%d bytes",
            server.name, result.returncode, len(result.stdout),
        )
        return ssh_result

    except subprocess.TimeoutExpired:
        raise SSHError(
            f"SSH command timed out after {effective_timeout}s on {server.name}",
            returncode=-1,
        )


def ssh_test(server: Server, ssh_key: Optional[str] = None) -> bool:
    """Test SSH connectivity to a server.

    Args:
        server: Target server.
        ssh_key: SSH key path override.

    Returns:
        True if connection works.
    """
    try:
        result = ssh_execute(
            server,
            'echo "jetbackup-remote-ok"',
            ssh_key=ssh_key,
            timeout=server.ssh_timeout,
        )
        return result.success and "jetbackup-remote-ok" in result.stdout
    except SSHError:
        return False
