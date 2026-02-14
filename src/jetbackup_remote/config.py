"""Configuration loader and validator for jetbackup-remote."""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .models import Server, Job, JobType


class ConfigError(Exception):
    """Raised when configuration is invalid."""
    pass


@dataclass
class DestinationConfig:
    """Destination lifecycle settings."""
    force_activate: bool = False
    skip_if_disabled: bool = True
    alert_days_without_jobs: int = 3


@dataclass
class NotificationConfig:
    """Email notification settings."""
    enabled: bool = False
    smtp_host: str = "localhost"
    smtp_port: int = 25
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_tls: bool = False
    from_address: str = "jetbackup-remote@localhost"
    to_addresses: list = field(default_factory=list)
    on_failure: bool = True
    on_timeout: bool = True
    on_complete: bool = False
    on_partial: bool = True


@dataclass
class OrchestratorConfig:
    """Orchestrator behavior settings."""
    poll_interval: int = 30
    job_timeout: int = 14400  # 4 hours
    startup_timeout: int = 120  # 2 min to wait for job to start after trigger
    lock_file: str = "/tmp/jetbackup-remote.lock"
    log_file: str = "/var/log/jetbackup-remote.log"
    log_max_bytes: int = 10485760  # 10 MB
    log_backup_count: int = 5


@dataclass
class Config:
    """Full application configuration."""
    servers: dict = field(default_factory=dict)
    jobs: list = field(default_factory=list)
    orchestrator: OrchestratorConfig = field(default_factory=OrchestratorConfig)
    notification: NotificationConfig = field(default_factory=NotificationConfig)
    destination: DestinationConfig = field(default_factory=DestinationConfig)
    ssh_key: Optional[str] = None

    @property
    def job_queue(self) -> list:
        """Return jobs ordered by server, then priority (higher first)."""
        return sorted(self.jobs, key=lambda j: (-j.priority, j.server_name))


def _parse_job_type(value: str) -> JobType:
    """Parse a job type string into JobType enum."""
    try:
        return JobType(value.lower())
    except ValueError:
        return JobType.OTHER


def _parse_server(name: str, data: dict) -> Server:
    """Parse a server definition from config dict."""
    if "host" not in data:
        raise ConfigError(f"Server '{name}' missing required field 'host'")

    return Server(
        name=name,
        host=data["host"],
        port=data.get("port", 51514),
        user=data.get("user", "root"),
        ssh_key=data.get("ssh_key"),
        ssh_timeout=data.get("ssh_timeout", 30),
        destination_id=data.get("destination_id"),
    )


def _parse_job(data: dict, server_names: set) -> Job:
    """Parse a job definition from config dict."""
    if "job_id" not in data:
        raise ConfigError("Job missing required field 'job_id'")
    if "server" not in data:
        raise ConfigError(f"Job '{data.get('job_id', '?')}' missing required field 'server'")

    server_name = data["server"]
    if server_name not in server_names:
        raise ConfigError(
            f"Job '{data['job_id']}' references unknown server '{server_name}'"
        )

    return Job(
        job_id=data["job_id"],
        server_name=server_name,
        label=data.get("label", ""),
        job_type=_parse_job_type(data.get("type", "other")),
        priority=data.get("priority", 0),
    )


def _parse_notification(data: dict) -> NotificationConfig:
    """Parse notification config."""
    return NotificationConfig(
        enabled=data.get("enabled", False),
        smtp_host=data.get("smtp_host", "localhost"),
        smtp_port=data.get("smtp_port", 25),
        smtp_user=data.get("smtp_user", ""),
        smtp_password=data.get("smtp_password", ""),
        smtp_tls=data.get("smtp_tls", False),
        from_address=data.get("from_address", "jetbackup-remote@localhost"),
        to_addresses=data.get("to_addresses", []),
        on_failure=data.get("on_failure", True),
        on_timeout=data.get("on_timeout", True),
        on_complete=data.get("on_complete", False),
        on_partial=data.get("on_partial", True),
    )


def _parse_orchestrator(data: dict) -> OrchestratorConfig:
    """Parse orchestrator config."""
    return OrchestratorConfig(
        poll_interval=data.get("poll_interval", 30),
        job_timeout=data.get("job_timeout", 14400),
        startup_timeout=data.get("startup_timeout", 120),
        lock_file=data.get("lock_file", "/tmp/jetbackup-remote.lock"),
        log_file=data.get("log_file", "/var/log/jetbackup-remote.log"),
        log_max_bytes=data.get("log_max_bytes", 10485760),
        log_backup_count=data.get("log_backup_count", 5),
    )


def _parse_destination(data: dict) -> DestinationConfig:
    """Parse destination lifecycle config."""
    return DestinationConfig(
        force_activate=data.get("force_activate", False),
        skip_if_disabled=data.get("skip_if_disabled", True),
        alert_days_without_jobs=data.get("alert_days_without_jobs", 3),
    )


def load_config(path: str) -> Config:
    """Load and validate configuration from a JSON file.

    Args:
        path: Path to config.json file.

    Returns:
        Validated Config instance.

    Raises:
        ConfigError: If config is missing, malformed, or invalid.
    """
    config_path = Path(path)
    if not config_path.exists():
        raise ConfigError(f"Config file not found: {path}")

    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ConfigError(f"Invalid JSON in config: {e}")

    if not isinstance(raw, dict):
        raise ConfigError("Config must be a JSON object")

    # Parse servers
    servers_raw = raw.get("servers", {})
    if not servers_raw:
        raise ConfigError("Config must define at least one server")

    servers = {}
    for name, sdata in servers_raw.items():
        servers[name] = _parse_server(name, sdata)

    server_names = set(servers.keys())

    # Parse jobs
    jobs_raw = raw.get("jobs", [])
    if not jobs_raw:
        raise ConfigError("Config must define at least one job")

    jobs = [_parse_job(jdata, server_names) for jdata in jobs_raw]

    # Parse optional sections
    orchestrator = _parse_orchestrator(raw.get("orchestrator", {}))
    notification = _parse_notification(raw.get("notification", {}))
    destination = _parse_destination(raw.get("destination", {}))
    ssh_key = raw.get("ssh_key")

    return Config(
        servers=servers,
        jobs=jobs,
        orchestrator=orchestrator,
        notification=notification,
        destination=destination,
        ssh_key=ssh_key,
    )


def validate_config(config: Config) -> list:
    """Run additional validation checks on a loaded config.

    Returns list of warning strings (empty = all good).
    """
    warnings = []

    # Check all jobs reference valid servers
    server_names = set(config.servers.keys())
    for job in config.jobs:
        if job.server_name not in server_names:
            warnings.append(f"Job {job.job_id} references unknown server {job.server_name}")

    # Check notification config consistency
    if config.notification.enabled and not config.notification.to_addresses:
        warnings.append("Notifications enabled but no to_addresses configured")

    if config.notification.smtp_tls and not config.notification.smtp_user:
        warnings.append("SMTP TLS enabled but no smtp_user configured")

    # Check for duplicate job IDs
    seen_ids = set()
    for job in config.jobs:
        if job.job_id in seen_ids:
            warnings.append(f"Duplicate job_id: {job.job_id}")
        seen_ids.add(job.job_id)

    # Check for missing destination_id
    servers_with_jobs = {j.server_name for j in config.jobs}
    for name in servers_with_jobs:
        server = config.servers.get(name)
        if server and not server.destination_id:
            warnings.append(
                f"Server '{name}' has no destination_id configured "
                f"(destination lifecycle management disabled)"
            )

    # Check for duplicate destination_ids
    seen_dest_ids = {}
    for name, server in config.servers.items():
        if server.destination_id:
            if server.destination_id in seen_dest_ids:
                warnings.append(
                    f"Duplicate destination_id '{server.destination_id}' "
                    f"on servers '{seen_dest_ids[server.destination_id]}' and '{name}'"
                )
            seen_dest_ids[server.destination_id] = name

    return warnings
