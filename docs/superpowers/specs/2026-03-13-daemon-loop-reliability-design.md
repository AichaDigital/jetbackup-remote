# jetbackup-remote v0.3.0 — Daemon Loop, Reliability & Observability

**Date:** 2026-03-13
**Author:** Abdelkarim Mateos
**Status:** Approved

## Problem Statement

jetbackup-remote v0.2.0 uses a systemd timer (`Type=oneshot`) to trigger daily backup orchestration. This architecture has three critical flaws exposed in the March 2-3 2026 incident:

1. **Cascading runs** — When a cycle takes >24h (common with Raspberry Pi USB bottleneck), the timer's `Persistent=true` setting (deployed config) queues the next run immediately after the first finishes. The repo was later patched to `Persistent=false`, but this causes the opposite problem: missed runs are silently skipped. Neither setting is correct for variable-duration workloads.
2. **Silent failure** — The timer was manually disabled on March 3 after a SIGTERM killed a running cycle. No alerting existed, so backups stopped for 10 days unnoticed.
3. **Unmanaged jobs** — Several JetBackup jobs assigned to the Raspberry destination were not in the orchestrator config, so they never ran (MySQL backups on 3 servers, Directories on central, entire kvm313 server).

## Design Decisions

### D1: Loop model — Python daemon with calculated pause

**Chosen:** `run_forever()` method in Orchestrator. `Type=simple` systemd service with `Restart=on-failure`.

**Rejected alternatives:**

- Shell wrapper (`while true; do run; sleep; done`) — Two signal layers, dynamic pause calculation awkward in bash.
- Timer with guard file — Neither `Persistent=true` (cascading) nor `Persistent=false` (silent skip) works for variable-duration workloads.

**Rationale:** Single process, single log, full control over SIGTERM handling during both execution and sleep phases.

### D2: Job timeout — 24h with abort

**Chosen:** Per-job timeout defaulting to 86400s (24h). On timeout: abort via `stopQueueGroup`, send email alert, move to next job.

**Rejected alternatives:**

- No timeout (let jobs run forever) — A stuck job would block the entire cycle indefinitely.
- Aggressive timeout with no abort (v0.2 deployed behavior) — Jobs continued running after "timeout", causing I/O contention cascade.

**Rationale:** 24h is generous enough for the USB-bottlenecked Raspberry Pi while catching genuinely stuck jobs. Per-job override allows shorter timeouts for database/config jobs.

### D3: Notifications — Email on failure, state file always

**Chosen:** SMTP email for immediate alerts + JSON state file for monitoring integration.

**Rejected:** Zabbix-only (not yet deployed for this infrastructure).

### D4: JetBackup Config jobs — excluded from orchestrator

**Chosen:** Not managed. They run opportunistically when the destination is active during other jobs.

**Rationale:** Trivially small (KB), not worth the orchestration overhead and extra cycle time.

## Architecture

### Loop lifecycle

`run_forever()` is a new public method on `Orchestrator`. It calls the existing `run()` method for each cycle (no duplication). Lock is acquired once at startup. `_global_preflight_check()` remains inside `run()`.

```
daemon starts
  → acquire lock (once, held for daemon lifetime)
  → write state file (daemon_pid, started_at)
  → send "daemon started" email (if SMTP fails: log error, continue — see SMTP failure handling)
  → loop:
      1. run() — full cycle (all servers, all jobs) — existing method, unchanged
      2. write state file (last_run results) — atomic write via temp+rename
      3. if failures/timeouts → send summary email
      4. calculate pause = max(target_interval - cycle_duration, min_pause)
         - target_interval: 86400s (24h), configurable
         - min_pause: 3600s (1h), configurable
      5. _interruptible_sleep(pause) — uses loop with time.sleep(30) checking _shutdown_requested
      6. goto 1
  → on SIGTERM:
      - if sleeping: wake up, send "daemon stopping" email, exit
      - if running cycle: set shutdown flag, current job finishes cleanly,
        remaining jobs/servers skipped, send email, exit
      - if inside _abort_running_job: abort-wait completes (max 60s), then shutdown proceeds
  → release lock
```

### `daemon` CLI subcommand

```
jetbackup-remote daemon [-c CONFIG] [--verbose]
```

- No `--server` / `--job` filters (daemon always runs all jobs)
- No `--dry-run` (daemon is always live)
- Calls `orchestrator.run_forever()`
- Handles lock acquisition, signal setup, logging setup
- On `LockError`: exit code 1 with message "Another instance is already running"

`jetbackup-remote run` remains unchanged (single cycle, backward compatible).

### Job timeout flow

```
job starts
  → resolve timeout: job.timeout or config.orchestrator.job_timeout
  → poll every 30s until running=False or timeout
  → if timeout (24h default):
      1. find running queue group for this job
      2. call stopQueueGroup (abort)
      3. poll up to 60s for confirmation (NOT interruptible by SIGTERM — must complete)
      4. send immediate timeout email
      5. mark job as TIMEOUT
      6. disable job, move to next
  → if completed normally:
      1. verify queue group status
      2. fetch logs if problematic
      3. mark as COMPLETED
```

**Timeout resolution in `_poll_completion_only`:**

```python
timeout = job.timeout if job.timeout else self.config.orchestrator.job_timeout
```

### Per-job timeout override

Jobs can specify a `timeout` field in config (seconds). If absent or 0, the global `job_timeout` applies. Per-job timeouts are validated independently: minimum 1800s, no maximum (the global MAX_JOB_TIMEOUT only applies to the global setting).

```json
{
    "job_id": "...",
    "server": "central",
    "label": "Mysql Xer",
    "type": "database",
    "timeout": 7200
}
```

### Notification events

| Event | Email | State file |
|-------|-------|------------|
| Daemon starts | Yes | pid, started_at |
| Daemon receives SIGTERM | Yes | — |
| Job timeout (abort) | Yes (immediate) | — |
| Job failure (SSH, API error) | Yes (immediate) | — |
| Cycle complete with errors | Yes (summary) | full results |
| Cycle complete without errors | No (configurable) | full results |

### SMTP failure handling

Email notifications must never block or crash the daemon. Policy:

- **All `send_*` calls wrapped in try/except** — SMTP errors are logged as WARNING, never raised.
- **No retry** — If SMTP is down, the email is lost. The state file remains as ground truth.
- **SMTP failure recorded in state file** — field `"last_notification_error"` with timestamp and error message. A monitoring tool can scrape this field.
- **No fallback channel** — When Zabbix is available, it will read the state file directly, providing an independent alerting path.

### State file

**Path:** `/var/lib/jetbackup-remote/last-run.json`

**Atomicity:** Written via temp file + `os.rename()` to prevent corruption on crash.

**Timestamps:** Always UTC in ISO 8601 format with `Z` suffix.

```json
{
    "version": "0.3.0",
    "daemon_pid": 12345,
    "daemon_started_at": "2026-03-13T23:01:00Z",
    "last_notification_error": null,
    "last_run": {
        "started_at": "2026-03-13T23:05:00Z",
        "finished_at": "2026-03-14T21:30:00Z",
        "duration_seconds": 80700,
        "total_jobs": 10,
        "completed": 8,
        "failed": 1,
        "timeout": 1,
        "skipped": 0,
        "success": false,
        "servers": {
            "central": {"status": "ok", "jobs": 3, "duration": 3420, "consecutive_failures": 0},
            "servidor02": {"status": "ok", "jobs": 3, "duration": 15900, "consecutive_failures": 0},
            "servidor20": {"status": "timeout", "jobs": 3, "duration": 86403, "consecutive_failures": 0},
            "servidor30": {"status": "ok", "jobs": 3, "duration": 31320, "consecutive_failures": 0},
            "kvm313": {"status": "failed", "jobs": 2, "duration": 0, "consecutive_failures": 3}
        }
    },
    "next_run_at": "2026-03-15T00:50:00Z"
}
```

**`consecutive_failures`:** Incremented each cycle a server is unreachable (SSH failure). Reset to 0 on success. Read from previous state file at cycle start. Allows future Zabbix triggers on sustained failures.

### `status` command enhancement

`jetbackup-remote status` reads state file + live server status:

```
Daemon: running (pid 12345, uptime 22h 30m)
Last cycle: 2026-03-14 22:30 UTC (8 OK, 1 TIMEOUT, 1 FAILED)
Next cycle: ~2026-03-15 01:50 UTC (in 3h 20m)

servidor02: SSH=OK JB5=OK DEST=DISABLED
  RasBackup (accounts): idle
  Directories-Ras (directories): idle
  Mysql02-Ras (database): idle
...
```

## Configuration Changes

### New section: `loop`

```json
"loop": {
    "target_interval": 86400,
    "min_pause": 3600
}
```

### New dataclass: `LoopConfig`

```python
@dataclass
class LoopConfig:
    target_interval: int = 86400   # 24h — target time between cycle starts
    min_pause: int = 3600          # 1h — minimum pause after a cycle
```

**Validation:**

- `target_interval` >= 3600 (minimum 1h)
- `min_pause` >= 600 (minimum 10 min)
- `min_pause` < `target_interval`

### Updated section: `orchestrator`

```json
"orchestrator": {
    "poll_interval": 30,
    "job_timeout": 86400,
    "startup_timeout": 120,
    "state_file": "/var/lib/jetbackup-remote/last-run.json",
    "lock_file": "/tmp/jetbackup-remote.lock",
    "log_file": "/var/log/jetbackup-remote.log",
    "log_max_bytes": 10485760,
    "log_backup_count": 5
}
```

**Validation change:** `MAX_JOB_TIMEOUT` raised to 172800 (48h) to give headroom above the 86400 default. Per-job `timeout` fields are validated separately: minimum 1800s, no maximum cap.

### Updated section: `notification`

```json
"notification": {
    "enabled": true,
    "smtp_host": "mail.castris.com",
    "smtp_port": 587,
    "smtp_tls": true,
    "smtp_user": "no-reply@castris.com",
    "smtp_password": "FROM_ENV:JETBACKUP_SMTP_PASSWORD",
    "from_address": "no-reply@castris.com",
    "to_addresses": ["abdel@xerintel.es"],
    "on_failure": true,
    "on_timeout": true,
    "on_complete": false,
    "on_partial": true,
    "on_daemon_lifecycle": true
}
```

**`FROM_ENV:` prefix convention:** If `smtp_password` starts with `FROM_ENV:`, the remainder is treated as an environment variable name. The config loader resolves it at parse time via `os.environ.get()`. If the variable is missing, `ConfigError` is raised.

**`on_partial`:** Existing boolean field in `NotificationConfig`. Retained from v0.2 — controls notifications for partial backup completions. Default: `true`.

**`on_daemon_lifecycle`:** New boolean field in `NotificationConfig`. Controls "daemon started" and "daemon stopping" emails. Default: `true`.

## New Jobs and Servers

### MySQL jobs to create (via WHM/JetBackup UI)

Create database backup jobs on servidor02, servidor20, servidor30 targeting the Raspberry destination:

- **Schedule:** 1x daily
- **Retention:** minimal (Raspberry is secondary destination)
- **Model:** similar to "Mysql Xer" on central

After creation, add their job_ids to `config.json`.

### Directories job on central

Create a Directories job on central targeting Raspberry destination:

- **Include list:** same as existing Directories job toward Xerbackup02
- **Schedule:** 1x daily

### kvm313 server

- **Host:** kvm313.xerintel.com
- **Port:** 51514
- **Panel:** DirectAdmin (not cPanel)
- **SSH key:** to determine (likely RSA based on Xerintel pattern)
- **Actions:**
  1. Register in sshctx
  2. Connect via direct SSH to inventory existing jobs and destination_id
  3. Add to `config.json` with discovered job_ids
  4. Note: JetBackup API commands may differ slightly on DirectAdmin (verify `jetbackup5api` availability)

## Systemd Changes

### Remove

- `/etc/systemd/system/jetbackup-remote.timer`

### Replace service

```ini
[Unit]
Description=JetBackup Remote Orchestrator Daemon
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 -m jetbackup_remote -c /etc/jetbackup-remote/config.json daemon
Restart=on-failure
RestartSec=300
Environment=PYTHONPATH=/opt/jetbackup-remote/src

ProtectSystem=strict
ReadWritePaths=/var/log /tmp /etc/jetbackup-remote /var/lib/jetbackup-remote
PrivateTmp=true
NoNewPrivileges=true
ProtectKernelModules=true
ProtectKernelTunables=true

[Install]
WantedBy=multi-user.target
```

**Notes:**

- `ProtectKernelModules` and `ProtectKernelTunables` retained from v0.2 service.
- `PrivateTmp=true` gives each service instance its own `/tmp`. The lock file at `/tmp/jetbackup-remote.lock` is private to the service — no stale lock issue on restart since the old `/tmp` is cleaned up by systemd.
- `Restart=on-failure` + `RestartSec=300`: if the daemon crashes, systemd restarts it after 5 min. The lock is released on crash (process death releases flock), so the new instance acquires it cleanly.

## Code Changes Summary

### New files

- None (all changes in existing modules)

### Modified files

| File | Changes |
|------|---------|
| `orchestrator.py` | Add `run_forever()`, `_interruptible_sleep()` (loop + `time.sleep(30)` checking `_shutdown_requested`), `_write_state()` (atomic temp+rename, UTC timestamps), connect notifier calls to timeout/failure/lifecycle events, pass `job.timeout` to `_poll_completion_only` |
| `cli.py` | Add `daemon` subcommand (no filters, no dry-run, calls `run_forever()`), update `status` to read state file + show daemon info |
| `config.py` | Add `LoopConfig` dataclass with validation, add `on_daemon_lifecycle` to `NotificationConfig`, add `state_file` to `OrchestratorConfig`, parse per-job `timeout` in `_parse_job()`, implement `FROM_ENV:` password resolution in `_parse_notification()`, raise `MAX_JOB_TIMEOUT` to 172800 |
| `models.py` | Add `timeout: Optional[int] = None` to `Job` dataclass |
| `notifier.py` | Add `send_timeout_alert()`, `send_cycle_summary()`, `send_daemon_lifecycle()` methods. All wrapped in try/except — SMTP errors logged, never raised |
| `pyproject.toml` | Version bump to 0.3.0, fix build-backend, remove obsolete license classifier |

### Test files

| File | Changes |
|------|---------|
| `test_orchestrator.py` | Tests for `run_forever()` (loop count, pause calculation, SIGTERM during sleep, SIGTERM during cycle), per-job timeout resolution, `_write_state()` atomicity, `_interruptible_sleep()` |
| `test_config.py` | Tests for `LoopConfig` parsing/validation, `FROM_ENV:` password resolution, per-job timeout validation, `on_daemon_lifecycle` flag |
| `test_notifier.py` | Tests for new `send_*` methods, SMTP failure handling (no exception raised) |
| `test_cli.py` | New file: tests for `daemon` subcommand argument parsing (if needed, or extend existing cli tests) |

### Systemd files

| File | Action |
|------|--------|
| `systemd/jetbackup-remote.service` | Replace (Type=simple, daemon command) |
| `systemd/jetbackup-remote.timer` | Delete |

## Deployment Procedure

1. Stop current timer and service on raspxer
2. Create MySQL jobs via WHM on servidor02, servidor20, servidor30
3. Create Directories job via WHM on central
4. Inventory kvm313 jobs via direct SSH
5. Register kvm313 in sshctx
6. Copy new code to `/opt/jetbackup-remote/`
7. Set env var `JETBACKUP_SMTP_PASSWORD` in service unit (via `Environment=` or systemd credentials)
8. Update `/etc/jetbackup-remote/config.json` (new jobs, kvm313, notification, loop section)
9. Create `/var/lib/jetbackup-remote/`
10. Remove timer unit, install new service unit
11. `systemctl daemon-reload && systemctl enable --now jetbackup-remote`
12. Verify daemon startup email received
13. Monitor first cycle via `jetbackup-remote status` and log

## Version

`0.2.0` → `0.3.0`
