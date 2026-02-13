"""FIFO orchestrator for serialized JetBackup5 job execution."""

import fcntl
import logging
import os
import signal
import time
from typing import Optional

from .config import Config
from .jetbackup_api import (
    JetBackupAPIError, is_job_running, run_job, test_connection,
)
from .models import Job, JobRun, JobStatus, RunResult, Server
from .ssh import SSHError

logger = logging.getLogger("jetbackup_remote.orchestrator")


class LockError(Exception):
    """Raised when the lock file cannot be acquired."""
    pass


class Orchestrator:
    """Serialized job executor with FIFO queue, polling, and lock file."""

    def __init__(self, config: Config):
        self.config = config
        self._lock_fd: Optional[int] = None
        self._shutdown_requested = False
        self._current_job: Optional[JobRun] = None

    def acquire_lock(self) -> None:
        """Acquire exclusive lock file to prevent concurrent runs.

        Raises:
            LockError: If lock is already held by another process.
        """
        lock_path = self.config.orchestrator.lock_file
        try:
            self._lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.write(self._lock_fd, f"{os.getpid()}\n".encode())
        except OSError:
            if self._lock_fd is not None:
                os.close(self._lock_fd)
                self._lock_fd = None
            raise LockError(
                f"Another instance is already running (lock: {lock_path})"
            )

    def release_lock(self) -> None:
        """Release the lock file."""
        if self._lock_fd is not None:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                os.close(self._lock_fd)
            except OSError:
                pass
            finally:
                self._lock_fd = None
            # Clean up lock file
            try:
                os.unlink(self.config.orchestrator.lock_file)
            except OSError:
                pass

    def install_signal_handlers(self) -> None:
        """Install SIGTERM/SIGINT handlers for graceful shutdown."""
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

    def _handle_signal(self, signum: int, frame) -> None:
        """Handle shutdown signal gracefully."""
        sig_name = signal.Signals(signum).name
        logger.warning("Received %s, requesting graceful shutdown...", sig_name)
        self._shutdown_requested = True

    def _get_server(self, server_name: str) -> Server:
        """Get server by name from config."""
        server = self.config.servers.get(server_name)
        if server is None:
            raise ValueError(f"Unknown server: {server_name}")
        return server

    def _build_queue(
        self,
        server_filter: Optional[str] = None,
        job_filter: Optional[str] = None,
    ) -> list:
        """Build the FIFO job queue from config.

        Args:
            server_filter: Only include jobs for this server.
            job_filter: Only include this specific job ID.

        Returns:
            Ordered list of Job objects.
        """
        jobs = self.config.job_queue

        if server_filter:
            jobs = [j for j in jobs if j.server_name == server_filter]

        if job_filter:
            jobs = [j for j in jobs if j.job_id == job_filter]

        return jobs

    def _wait_for_startup(
        self,
        server: Server,
        job: Job,
    ) -> bool:
        """Wait for JetBackup to mark the job as running after trigger.

        After runBackupJobManually, JetBackup queues the job asynchronously.
        There is a race window where 'running' is still False. This method
        polls until 'running=True' is observed, confirming the job started.

        Args:
            server: Target server.
            job: The triggered job.

        Returns:
            True if job started (running=True seen).
            False if startup_timeout expired without seeing running=True.
        """
        startup_timeout = self.config.orchestrator.startup_timeout
        poll_interval = min(self.config.orchestrator.poll_interval, 10)
        start_time = time.time()

        logger.info(
            "Waiting for %s on %s to start (up to %ds)...",
            job.display_name, server.name, startup_timeout,
        )

        while True:
            if self._shutdown_requested:
                return False

            elapsed = time.time() - start_time
            if elapsed >= startup_timeout:
                logger.warning(
                    "Job %s on %s did not start within %ds",
                    job.display_name, server.name, startup_timeout,
                )
                return False

            try:
                running = is_job_running(
                    server, job.job_id, ssh_key=self.config.ssh_key,
                )
            except (JetBackupAPIError, SSHError) as e:
                logger.warning(
                    "Startup poll error for %s on %s: %s (will retry)",
                    job.display_name, server.name, e,
                )
                time.sleep(poll_interval)
                continue

            if running:
                logger.info(
                    "Job %s on %s is now running (started after %.0fs)",
                    job.display_name, server.name, elapsed,
                )
                return True

            time.sleep(poll_interval)

    def _poll_completion_only(
        self,
        server: Server,
        job: Job,
        job_run: JobRun,
    ) -> None:
        """Poll until running=False or timeout. Used when job is known to be running.

        Does NOT abort the job on timeout (would corrupt backup).

        Args:
            server: Target server.
            job: The job being polled.
            job_run: JobRun state tracker.
        """
        poll_interval = self.config.orchestrator.poll_interval
        timeout = self.config.orchestrator.job_timeout
        start_time = time.time()

        while True:
            if self._shutdown_requested:
                logger.warning(
                    "Shutdown requested during poll for %s on %s",
                    job.display_name, server.name,
                )
                job_run.skip("Shutdown requested during polling")
                return

            elapsed = time.time() - start_time
            if elapsed >= timeout:
                logger.warning(
                    "Job %s on %s exceeded timeout (%ds). "
                    "NOT aborting (would corrupt backup). Moving to next job.",
                    job.display_name, server.name, timeout,
                )
                job_run.timeout()
                return

            try:
                still_running = is_job_running(
                    server, job.job_id, ssh_key=self.config.ssh_key,
                )
            except (JetBackupAPIError, SSHError) as e:
                logger.warning(
                    "Poll error for %s on %s: %s (will retry)",
                    job.display_name, server.name, e,
                )
                time.sleep(poll_interval)
                continue

            if not still_running:
                logger.info(
                    "Job %s on %s completed (%.0fs)",
                    job.display_name, server.name, elapsed,
                )
                job_run.complete()
                return

            remaining = timeout - elapsed
            logger.debug(
                "Job %s on %s still running (%.0fs elapsed, %.0fs remaining)",
                job.display_name, server.name, elapsed, remaining,
            )
            time.sleep(poll_interval)

    def _poll_job_completion(
        self,
        server: Server,
        job: Job,
        job_run: JobRun,
    ) -> None:
        """Two-phase poll: wait for startup, then wait for completion.

        Phase 1 (startup): After trigger, wait until running=True is observed.
        Phase 2 (completion): Once running, wait until running=False.

        Does NOT abort the job on timeout (would corrupt backup).

        Args:
            server: Target server.
            job: The job being polled.
            job_run: JobRun state tracker.
        """
        # Phase 1: wait for job to actually start
        started = self._wait_for_startup(server, job)

        if self._shutdown_requested:
            job_run.skip("Shutdown requested during startup wait")
            return

        if not started:
            job_run.fail(
                f"Job did not start within {self.config.orchestrator.startup_timeout}s "
                f"after trigger"
            )
            return

        # Phase 2: poll until job finishes
        self._poll_completion_only(server, job, job_run)

    def _execute_job(self, job: Job) -> JobRun:
        """Execute a single job: trigger + poll.

        Args:
            job: Job to execute.

        Returns:
            JobRun with final status.
        """
        job_run = JobRun(job=job)
        server = self._get_server(job.server_name)

        logger.info(
            "Starting job %s (%s) on %s",
            job.display_name, job.job_id, server.name,
        )

        # Test connectivity
        try:
            conn = test_connection(server, ssh_key=self.config.ssh_key)
            if not conn["ssh_ok"]:
                msg = f"SSH unreachable: {conn.get('error', 'unknown')}"
                logger.error(msg)
                job_run.skip(msg)
                return job_run
        except Exception as e:
            msg = f"Connection test failed: {e}"
            logger.error(msg)
            job_run.skip(msg)
            return job_run

        job_run.start()
        self._current_job = job_run

        # Check if already running
        try:
            already_running = is_job_running(
                server, job.job_id, ssh_key=self.config.ssh_key,
            )
        except (JetBackupAPIError, SSHError) as e:
            job_run.fail(f"Failed to check job status: {e}")
            self._current_job = None
            return job_run

        if already_running:
            logger.info(
                "Job %s on %s already running, waiting for completion...",
                job.display_name, server.name,
            )
            # Skip startup wait, go directly to completion poll
            self._poll_completion_only(server, job, job_run)
        else:
            # Trigger the job
            try:
                run_job(server, job.job_id, ssh_key=self.config.ssh_key)
            except (JetBackupAPIError, SSHError) as e:
                job_run.fail(f"Failed to trigger job: {e}")
                self._current_job = None
                return job_run

            # Two-phase poll: startup + completion
            self._poll_job_completion(server, job, job_run)

        self._current_job = None
        return job_run

    def _execute_job_dry_run(self, job: Job) -> JobRun:
        """Simulate job execution without triggering.

        Args:
            job: Job to simulate.

        Returns:
            JobRun marked as completed.
        """
        job_run = JobRun(job=job)
        server = self._get_server(job.server_name)

        logger.info(
            "[DRY RUN] Would execute job %s (%s) on %s (%s:%d)",
            job.display_name, job.job_id, server.name, server.host, server.port,
        )
        job_run.start()
        job_run.complete()
        return job_run

    def run(
        self,
        dry_run: bool = False,
        server_filter: Optional[str] = None,
        job_filter: Optional[str] = None,
    ) -> RunResult:
        """Execute the full orchestration run.

        Args:
            dry_run: Simulate without triggering jobs.
            server_filter: Only run jobs for this server.
            job_filter: Only run this specific job.

        Returns:
            RunResult with summary of all jobs.
        """
        result = RunResult(dry_run=dry_run)
        queue = self._build_queue(server_filter, job_filter)

        if not queue:
            logger.warning("No jobs in queue (filters: server=%s, job=%s)",
                           server_filter, job_filter)
            return result

        logger.info(
            "Starting %s run with %d job(s)%s",
            "dry-run" if dry_run else "live",
            len(queue),
            f" (server={server_filter})" if server_filter else "",
        )

        result.start()

        for i, job in enumerate(queue, 1):
            if self._shutdown_requested:
                logger.warning("Shutdown requested, skipping remaining jobs")
                # Mark remaining as skipped
                for remaining_job in queue[i - 1:]:
                    jr = JobRun(job=remaining_job)
                    jr.skip("Shutdown requested")
                    result.add_job_run(jr)
                break

            logger.info("Job %d/%d: %s on %s", i, len(queue),
                        job.display_name, job.server_name)

            if dry_run:
                job_run = self._execute_job_dry_run(job)
            else:
                job_run = self._execute_job(job)

            result.add_job_run(job_run)

            log_level = logging.INFO if job_run.status == JobStatus.COMPLETED else logging.WARNING
            logger.log(
                log_level,
                "Job %d/%d %s: %s (%s)",
                i, len(queue), job_run.status.value,
                job.display_name, job_run.duration_human,
            )

        result.finish()
        logger.info("Run complete: %s", result.summary_line())
        return result
