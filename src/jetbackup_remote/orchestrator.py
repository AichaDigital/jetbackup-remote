"""Server-grouped orchestrator for serialized JetBackup5 job execution with
destination lifecycle management."""

import fcntl
import logging
import os
import signal
import time
from collections import OrderedDict
from typing import Optional

from .config import Config
from .jetbackup_api import (
    JetBackupAPIError,
    clear_queue,
    get_queue_group,
    is_destination_disabled,
    is_job_running,
    list_queue_groups,
    run_job,
    set_destination_state,
    set_job_enabled,
    stop_queue_group,
    test_connection,
)
from .models import (
    Job, JobRun, JobStatus, QueueGroupStatus,
    RunResult, Server, ServerRun,
)
from .ssh import SSHError

logger = logging.getLogger("jetbackup_remote.orchestrator")


class LockError(Exception):
    """Raised when the lock file cannot be acquired."""
    pass


class Orchestrator:
    """Server-grouped job executor with destination lifecycle management."""

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

    # --- Queue building ---

    def _build_queue(
        self,
        server_filter: Optional[str] = None,
        job_filter: Optional[str] = None,
    ) -> list:
        """Build the FIFO job queue from config (backward compatible).

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

    def _build_server_groups(
        self,
        server_filter: Optional[str] = None,
        job_filter: Optional[str] = None,
    ) -> OrderedDict:
        """Build server-grouped job queue.

        Returns:
            OrderedDict mapping server_name → list[Job], ordered by
            first job priority within each server group.
        """
        queue = self._build_queue(server_filter, job_filter)
        groups = OrderedDict()
        for job in queue:
            if job.server_name not in groups:
                groups[job.server_name] = []
            groups[job.server_name].append(job)
        return groups

    # --- Polling ---

    def _wait_for_startup(
        self,
        server: Server,
        job: Job,
    ) -> bool:
        """Wait for JetBackup to mark the job as running after trigger.

        After runBackupJobManually, JetBackup queues the job asynchronously.
        There is a race window where 'running' is still False. This method
        polls until 'running=True' is observed, confirming the job started.

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

    # --- Destination lifecycle ---

    def _pre_flight_check(self, server: Server) -> dict:
        """Test SSH + JetBackup connectivity and destination state.

        Returns:
            Dict with ssh_ok, jetbackup_ok, destination_state, error.
        """
        result = test_connection(server, ssh_key=self.config.ssh_key)

        if result["ssh_ok"] and result["jetbackup_ok"] and server.destination_id:
            try:
                disabled = is_destination_disabled(
                    server, server.destination_id, ssh_key=self.config.ssh_key,
                )
                result["destination_disabled"] = disabled
            except (JetBackupAPIError, SSHError) as e:
                result["destination_disabled"] = None
                if not result.get("error"):
                    result["error"] = f"Destination check failed: {e}"

        return result

    def _check_queue_clean(
        self,
        server: Server,
        destination_id: Optional[str],
    ) -> tuple:
        """Check for orphan queue groups and kill them.

        Returns:
            (is_clean, killed_count)
        """
        try:
            groups = list_queue_groups(server, ssh_key=self.config.ssh_key)
        except (JetBackupAPIError, SSHError) as e:
            logger.warning("Failed to check queue on %s: %s", server.name, e)
            return True, 0

        # Filter to running groups (status=2)
        running = [g for g in groups if g.get("status") == QueueGroupStatus.RUNNING]
        if not running:
            return True, 0

        killed = 0
        for group in running:
            group_id = group.get("_id", "unknown")
            logger.warning(
                "Orphan queue group %s found on %s (status=%s), stopping...",
                group_id, server.name, group.get("status"),
            )
            try:
                stop_queue_group(server, group_id, ssh_key=self.config.ssh_key)
                killed += 1
            except (JetBackupAPIError, SSHError) as e:
                logger.error("Failed to stop orphan queue group %s: %s", group_id, e)

        return killed == len(running), killed

    def _activate_destination(self, server: Server, server_run: ServerRun) -> bool:
        """Activate (enable) the RASP destination on a server.

        Returns:
            True if activation succeeded or was unnecessary.
        """
        if not server.destination_id:
            logger.debug("No destination_id for %s, skipping activation", server.name)
            return True

        try:
            set_destination_state(
                server, server.destination_id, disabled=False,
                ssh_key=self.config.ssh_key,
            )
            server_run.destination_activated = True
            logger.info("Activated destination %s on %s", server.destination_id, server.name)
            return True
        except (JetBackupAPIError, SSHError) as e:
            msg = f"Failed to activate destination on {server.name}: {e}"
            logger.error(msg)
            server_run.activation_error = msg
            return False

    def _deactivate_destination(self, server: Server, server_run: ServerRun) -> bool:
        """Deactivate (disable) the RASP destination on a server.

        ALWAYS called in finally block. Failure is CRITICAL.

        Returns:
            True if deactivation succeeded.
        """
        if not server.destination_id:
            return True

        if not server_run.destination_activated:
            # We never activated it, no need to deactivate
            return True

        try:
            set_destination_state(
                server, server.destination_id, disabled=True,
                ssh_key=self.config.ssh_key,
            )
            server_run.destination_deactivated = True
            logger.info("Deactivated destination %s on %s", server.destination_id, server.name)
            return True
        except (JetBackupAPIError, SSHError) as e:
            msg = f"CRITICAL: Failed to deactivate destination on {server.name}: {e}"
            logger.critical(msg)
            server_run.deactivation_error = msg
            return False

    # --- Job lifecycle ---

    def _enable_job(self, server: Server, job: Job) -> bool:
        """Enable a job before execution.

        Returns:
            True if the job was enabled successfully.
        """
        try:
            set_job_enabled(server, job.job_id, enabled=True, ssh_key=self.config.ssh_key)
            logger.info("Enabled job %s on %s", job.display_name, server.name)
            return True
        except (JetBackupAPIError, SSHError) as e:
            logger.error("Failed to enable job %s on %s: %s", job.display_name, server.name, e)
            return False

    def _disable_job(self, server: Server, job: Job) -> bool:
        """Disable a job after execution. ALWAYS called, even on error.

        Returns:
            True if the job was disabled successfully.
        """
        try:
            set_job_enabled(server, job.job_id, enabled=False, ssh_key=self.config.ssh_key)
            logger.info("Disabled job %s on %s", job.display_name, server.name)
            return True
        except (JetBackupAPIError, SSHError) as e:
            logger.error(
                "Failed to disable job %s on %s: %s (job left ENABLED!)",
                job.display_name, server.name, e,
            )
            return False

    # --- Verification ---

    def _find_queue_group_for_job(
        self,
        server: Server,
        job: Job,
        job_run: JobRun,
    ) -> Optional[dict]:
        """Find the queue group created by the most recent run of this job.

        Searches recent queue groups for one matching the job_id.

        Returns:
            Queue group dict or None.
        """
        try:
            groups = list_queue_groups(server, ssh_key=self.config.ssh_key)
        except (JetBackupAPIError, SSHError) as e:
            logger.warning("Failed to list queue groups for verification on %s: %s", server.name, e)
            return None

        # Find the most recent completed group for this job
        # Queue group stores job info in "data._id" (not top-level "job_id")
        for group in groups:
            group_data = group.get("data", {})
            if isinstance(group_data, dict) and group_data.get("_id") == job.job_id:
                return group

        return None

    def _verify_job_outcome(
        self,
        server: Server,
        job: Job,
        job_run: JobRun,
    ) -> None:
        """Verify job outcome by checking queue group status and fetching logs.

        Modifies job_run in-place with verification data.
        """
        group = self._find_queue_group_for_job(server, job, job_run)
        if group is None:
            logger.warning(
                "No queue group found for job %s on %s, skipping verification",
                job.display_name, server.name,
            )
            return

        group_id = group.get("_id")
        status_code = group.get("status")
        job_run.queue_group_id = group_id

        try:
            status = QueueGroupStatus(status_code)
            job_run.queue_group_status = status
            logger.info(
                "Job %s on %s: queue group %s status=%s (%d)",
                job.display_name, server.name, group_id, status.name, status_code,
            )
        except ValueError:
            logger.warning(
                "Job %s on %s: unknown queue group status %s",
                job.display_name, server.name, status_code,
            )
            return

        # Fetch log contents for problematic jobs
        if status.is_problematic:
            try:
                detailed = get_queue_group(
                    server, group_id, get_log_contents=True,
                    ssh_key=self.config.ssh_key,
                )
                log_contents = detailed.get("log_contents", "")
                if log_contents:
                    job_run.log_contents = log_contents
                    logger.info(
                        "Fetched log contents for %s on %s (%d chars)",
                        job.display_name, server.name, len(log_contents),
                    )
            except (JetBackupAPIError, SSHError) as e:
                logger.warning(
                    "Failed to fetch log for %s on %s: %s",
                    job.display_name, server.name, e,
                )

    # --- Scheduler interference ---

    def _check_scheduler_interference(
        self,
        server: Server,
        destination_id: Optional[str],
        our_queue_group_ids: set,
    ) -> list:
        """Check for queue groups we didn't create (scheduler interference).

        Returns:
            List of foreign queue group IDs found.
        """
        try:
            groups = list_queue_groups(server, ssh_key=self.config.ssh_key)
        except (JetBackupAPIError, SSHError) as e:
            logger.warning("Failed to check scheduler interference on %s: %s", server.name, e)
            return []

        running = [
            g for g in groups
            if g.get("status") == QueueGroupStatus.RUNNING
            and g.get("_id") not in our_queue_group_ids
        ]

        if running:
            foreign_ids = [g.get("_id", "unknown") for g in running]
            logger.warning(
                "Scheduler interference detected on %s: %d foreign queue groups: %s",
                server.name, len(running), ", ".join(foreign_ids),
            )
            # Stop foreign groups to prevent concurrent writes to NAS
            for group in running:
                gid = group.get("_id")
                if gid:
                    try:
                        stop_queue_group(server, gid, ssh_key=self.config.ssh_key)
                        logger.warning("Stopped foreign queue group %s on %s", gid, server.name)
                    except (JetBackupAPIError, SSHError) as e:
                        logger.error("Failed to stop foreign queue group %s: %s", gid, e)

            return foreign_ids

        return []

    # --- Server processing ---

    def _process_server(
        self,
        server: Server,
        jobs: list,
        force_activate: bool = False,
    ) -> ServerRun:
        """Process all jobs for a single server with destination lifecycle.

        Args:
            server: Target server.
            jobs: List of Job objects to execute.
            force_activate: Override skip_if_disabled.

        Returns:
            ServerRun with results.
        """
        server_run = ServerRun(
            server_name=server.name,
            destination_id=server.destination_id,
        )
        our_queue_group_ids = set()

        # Pre-flight check
        conn = self._pre_flight_check(server)
        if not conn["ssh_ok"]:
            msg = f"SSH unreachable: {conn.get('error', 'unknown')}"
            logger.error(msg)
            for job in jobs:
                jr = JobRun(job=job)
                jr.skip(msg)
                server_run.job_runs.append(jr)
            return server_run

        # Destination lifecycle: decide whether to activate
        needs_activation = server.destination_id is not None
        dest_disabled = conn.get("destination_disabled")

        if needs_activation and dest_disabled is True:
            if not force_activate and self.config.destination.skip_if_disabled:
                msg = (
                    f"Destination {server.destination_id} on {server.name} "
                    f"is disabled and skip_if_disabled=True. Skipping server."
                )
                logger.warning(msg)
                for job in jobs:
                    jr = JobRun(job=job)
                    jr.skip(msg)
                    server_run.job_runs.append(jr)
                return server_run

        try:
            # Clean orphan queue entries
            if server.destination_id:
                _, killed = self._check_queue_clean(server, server.destination_id)
                server_run.orphan_queue_groups_killed = killed

            # Activate destination
            if needs_activation:
                if not self._activate_destination(server, server_run):
                    msg = f"Destination activation failed on {server.name}"
                    for job in jobs:
                        jr = JobRun(job=job)
                        jr.skip(msg)
                        server_run.job_runs.append(jr)
                    return server_run

            # Execute each job
            for i, job in enumerate(jobs, 1):
                if self._shutdown_requested:
                    jr = JobRun(job=job)
                    jr.skip("Shutdown requested")
                    server_run.job_runs.append(jr)
                    continue

                logger.info(
                    "Job %d/%d on %s: %s",
                    i, len(jobs), server.name, job.display_name,
                )

                job_run = self._execute_job_with_lifecycle(server, job)
                server_run.job_runs.append(job_run)

                # Track queue group IDs for scheduler interference check
                if job_run.queue_group_id:
                    our_queue_group_ids.add(job_run.queue_group_id)

            # Check for scheduler interference
            if server.destination_id:
                self._check_scheduler_interference(
                    server, server.destination_id, our_queue_group_ids,
                )

        finally:
            # ALWAYS deactivate destination
            if needs_activation:
                self._deactivate_destination(server, server_run)

        return server_run

    def _execute_job_with_lifecycle(self, server: Server, job: Job) -> JobRun:
        """Execute a single job with enable/disable lifecycle.

        Args:
            server: Target server.
            job: Job to execute.

        Returns:
            JobRun with final status.
        """
        job_run = JobRun(job=job)

        # Enable job
        if not self._enable_job(server, job):
            job_run.skip(f"Failed to enable job {job.display_name}")
            return job_run

        try:
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

            # Verify outcome if job completed
            if job_run.status == JobStatus.COMPLETED:
                self._verify_job_outcome(server, job, job_run)

            self._current_job = None

        finally:
            # ALWAYS disable job
            if not self._disable_job(server, job):
                if job_run.error_message:
                    job_run.error_message += " | Job left ENABLED after execution"
                else:
                    job_run.error_message = "Job left ENABLED after execution"

        return job_run

    # --- Legacy single-job executor (backward compatible) ---

    def _execute_job(self, job: Job) -> JobRun:
        """Execute a single job: trigger + poll (legacy, no lifecycle).

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
        force_activate: Optional[bool] = None,
    ) -> RunResult:
        """Execute the full orchestration run with server-grouped processing.

        Args:
            dry_run: Simulate without triggering jobs.
            server_filter: Only run jobs for this server.
            job_filter: Only run this specific job.
            force_activate: Override destination skip_if_disabled (CLI flag).

        Returns:
            RunResult with summary of all jobs.
        """
        result = RunResult(dry_run=dry_run)
        server_groups = self._build_server_groups(server_filter, job_filter)

        if not server_groups:
            logger.warning("No jobs in queue (filters: server=%s, job=%s)",
                           server_filter, job_filter)
            return result

        total_jobs = sum(len(jobs) for jobs in server_groups.values())
        logger.info(
            "Starting %s run with %d job(s) across %d server(s)%s",
            "dry-run" if dry_run else "live",
            total_jobs,
            len(server_groups),
            f" (server={server_filter})" if server_filter else "",
        )

        result.start()

        # Determine force_activate: CLI override > config
        effective_force = force_activate if force_activate is not None else self.config.destination.force_activate

        job_counter = 0
        for server_name, jobs in server_groups.items():
            if self._shutdown_requested:
                logger.warning("Shutdown requested, skipping remaining servers")
                for job in jobs:
                    jr = JobRun(job=job)
                    jr.skip("Shutdown requested")
                    result.add_job_run(jr)
                continue

            logger.info(
                "Processing server %s (%d jobs)",
                server_name, len(jobs),
            )

            if dry_run:
                for job in jobs:
                    job_counter += 1
                    job_run = self._execute_job_dry_run(job)
                    result.add_job_run(job_run)
                    logger.info(
                        "Job %d/%d completed: %s (%s)",
                        job_counter, total_jobs, job.display_name, job_run.duration_human,
                    )
            else:
                server = self._get_server(server_name)
                server_run = self._process_server(server, jobs, force_activate=effective_force)
                result.add_server_run(server_run)

                for job_run in server_run.job_runs:
                    result.add_job_run(job_run)
                    job_counter += 1
                    log_level = logging.INFO if job_run.status == JobStatus.COMPLETED else logging.WARNING
                    logger.log(
                        log_level,
                        "Job %d/%d %s: %s on %s (%s)",
                        job_counter, total_jobs, job_run.status.value,
                        job_run.job.display_name, server_name, job_run.duration_human,
                    )

        result.finish()
        logger.info("Run complete: %s", result.summary_line())
        return result
