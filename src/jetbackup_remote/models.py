"""Data models for jetbackup-remote."""

from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Optional
import time


class JobStatus(Enum):
    """Status of a backup job."""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    SKIPPED = "skipped"


class QueueGroupStatus(IntEnum):
    """JetBackup queue group status codes."""
    RUNNING = 2
    COMPLETED = 100


class JobType(Enum):
    """Type of backup job."""
    ACCOUNTS = "accounts"
    DIRECTORIES = "directories"
    DATABASE = "database"
    OTHER = "other"


@dataclass
class Server:
    """A remote server with JetBackup5 installed."""
    name: str
    host: str
    port: int = 51514
    user: str = "root"
    ssh_key: Optional[str] = None
    ssh_timeout: int = 30

    def ssh_target(self) -> str:
        """Return user@host string."""
        return f"{self.user}@{self.host}"


@dataclass
class Job:
    """A JetBackup5 backup job on a remote server."""
    job_id: str
    server_name: str
    label: str = ""
    job_type: JobType = JobType.OTHER
    priority: int = 0

    @property
    def display_name(self) -> str:
        """Human-readable name for this job."""
        return self.label or self.job_id


@dataclass
class JobRun:
    """State of a single job execution within a run."""
    job: Job
    status: JobStatus = JobStatus.PENDING
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    error_message: Optional[str] = None

    def start(self) -> None:
        """Mark job as running."""
        self.status = JobStatus.RUNNING
        self.started_at = time.time()

    def complete(self) -> None:
        """Mark job as completed."""
        self.status = JobStatus.COMPLETED
        self.finished_at = time.time()

    def fail(self, message: str) -> None:
        """Mark job as failed."""
        self.status = JobStatus.FAILED
        self.finished_at = time.time()
        self.error_message = message

    def timeout(self) -> None:
        """Mark job as timed out (non-aborting)."""
        self.status = JobStatus.TIMEOUT
        self.finished_at = time.time()
        self.error_message = "Job exceeded timeout limit"

    def skip(self, message: str) -> None:
        """Mark job as skipped."""
        self.status = JobStatus.SKIPPED
        self.finished_at = time.time()
        self.error_message = message

    @property
    def duration_seconds(self) -> Optional[float]:
        """Duration in seconds, or None if not started/finished."""
        if self.started_at is None:
            return None
        end = self.finished_at or time.time()
        return end - self.started_at

    @property
    def duration_human(self) -> str:
        """Human-readable duration."""
        seconds = self.duration_seconds
        if seconds is None:
            return "-"
        hours, remainder = divmod(int(seconds), 3600)
        minutes, secs = divmod(remainder, 60)
        if hours:
            return f"{hours}h {minutes}m {secs}s"
        if minutes:
            return f"{minutes}m {secs}s"
        return f"{secs}s"


@dataclass
class RunResult:
    """Summary of a complete orchestration run."""
    jobs: list = field(default_factory=list)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    dry_run: bool = False

    def start(self) -> None:
        """Mark run as started."""
        self.started_at = time.time()

    def finish(self) -> None:
        """Mark run as finished."""
        self.finished_at = time.time()

    def add_job_run(self, job_run: JobRun) -> None:
        """Add a job run to the result."""
        self.jobs.append(job_run)

    @property
    def total(self) -> int:
        return len(self.jobs)

    @property
    def completed(self) -> int:
        return sum(1 for j in self.jobs if j.status == JobStatus.COMPLETED)

    @property
    def failed(self) -> int:
        return sum(1 for j in self.jobs if j.status == JobStatus.FAILED)

    @property
    def timed_out(self) -> int:
        return sum(1 for j in self.jobs if j.status == JobStatus.TIMEOUT)

    @property
    def skipped(self) -> int:
        return sum(1 for j in self.jobs if j.status == JobStatus.SKIPPED)

    @property
    def success(self) -> bool:
        """True if all jobs completed without failure."""
        return self.failed == 0 and self.timed_out == 0

    @property
    def duration_seconds(self) -> Optional[float]:
        if self.started_at is None:
            return None
        end = self.finished_at or time.time()
        return end - self.started_at

    def summary_line(self) -> str:
        """One-line summary of the run."""
        parts = [f"Total: {self.total}"]
        if self.completed:
            parts.append(f"OK: {self.completed}")
        if self.failed:
            parts.append(f"FAILED: {self.failed}")
        if self.timed_out:
            parts.append(f"TIMEOUT: {self.timed_out}")
        if self.skipped:
            parts.append(f"SKIPPED: {self.skipped}")
        return " | ".join(parts)
