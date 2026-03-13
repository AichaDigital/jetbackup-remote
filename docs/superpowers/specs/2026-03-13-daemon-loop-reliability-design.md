# jetbackup-remote v0.3.0 — Daemon Loop, Reliability & Observability

**Date:** 2026-03-13
**Author:** Abdelkarim Mateos
**Status:** Approved

## Problem Statement

jetbackup-remote v0.2.0 uses a systemd timer (daily at 00:01) with `Type=oneshot` and `Persistent=true`. This architecture has three critical flaws exposed in the March 2-3 2026 incident:

1. **Cascading runs** — When a cycle takes >24h (common with Raspberry Pi USB bottleneck), `Persistent=true` queues the next run immediately. This creates an infinite loop of overlapping cycles.
2. **Silent failure** — The timer was manually disabled on March 3 after a SIGTERM killed a running cycle. No alerting existed, so backups stopped for 10 days unnoticed.
3. **Unmanaged jobs** — Several JetBackup jobs assigned to the Raspberry destination were not in the orchestrator config, so they never ran (MySQL backups on 3 servers, Directories on central, entire kvm313 server).

## Design Decisions

### D1: Loop model — Python daemon with calculated pause

**Chosen:** `run_forever()` method in Orchestrator. `Type=simple` systemd service with `Restart=on-failure`.

**Rejected alternatives:**

- Shell wrapper (`while true; do run; sleep; done`) — Two signal layers, dynamic pause calculation awkward in bash.
- Timer with guard file — Contradicts the goal; interval still depends on timer, not cycle duration.

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

```
daemon starts
  → write state file (daemon_pid, started_at)
  → send "daemon started" email
  → loop:
      1. run() — full cycle (all servers, all jobs)
      2. write state file (last_run results)
      3. if failures/timeouts → send summary email
      4. calculate pause = max(target_interval - cycle_duration, min_pause)
         - target_interval: 86400s (24h), configurable
         - min_pause: 3600s (1h), configurable
      5. sleep (interruptible — checks SIGTERM every 30s)
      6. goto 1
  → on SIGTERM:
      - if sleeping: wake up, send "daemon stopping" email, exit
      - if running cycle: set shutdown flag, current job finishes cleanly,
        remaining jobs/servers skipped, send email, exit
```

### Job timeout flow

```
job starts
  → poll every 30s until running=False or timeout
  → if timeout (24h default):
      1. find running queue group for this job
      2. call stopQueueGroup (abort)
      3. poll up to 60s for confirmation
      4. send immediate timeout email
      5. mark job as TIMEOUT
      6. disable job, move to next
  → if completed normally:
      1. verify queue group status
      2. fetch logs if problematic
      3. mark as COMPLETED
```

### Per-job timeout override

Jobs can specify a `timeout` field in config. If absent, the global `job_timeout` applies.

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

### State file

**Path:** `/var/lib/jetbackup-remote/last-run.json`

```json
{
    "version": "0.3.0",
    "daemon_pid": 12345,
    "daemon_started_at": "2026-03-14T00:01:00+01:00",
    "last_run": {
        "started_at": "2026-03-14T00:05:00+01:00",
        "finished_at": "2026-03-14T22:30:00+01:00",
        "duration_seconds": 80700,
        "total_jobs": 10,
        "completed": 8,
        "failed": 1,
        "timeout": 1,
        "skipped": 0,
        "success": false,
        "servers": {
            "central": {"status": "ok", "jobs": 3, "duration": 3420},
            "servidor02": {"status": "ok", "jobs": 3, "duration": 15900},
            "servidor20": {"status": "timeout", "jobs": 3, "duration": 86403},
            "servidor30": {"status": "ok", "jobs": 3, "duration": 31320},
            "kvm313": {"status": "failed", "jobs": 2, "duration": 0}
        }
    },
    "next_run_at": "2026-03-15T01:50:00+01:00"
}
```

### `status` command enhancement

`jetbackup-remote status` reads state file + live server status:

```
Daemon: running (pid 12345, uptime 22h 30m)
Last cycle: 2026-03-14 22:30 (8 OK, 1 TIMEOUT, 1 FAILED)
Next cycle: ~2026-03-15 01:50 (in 3h 20m)

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

### Updated section: `notification`

```json
"notification": {
    "enabled": true,
    "smtp_host": "mail.castris.com",
    "smtp_port": 587,
    "smtp_tls": true,
    "smtp_user": "no-reply@castris.com",
    "smtp_password": "FROM_ENV",
    "from_address": "no-reply@castris.com",
    "to_addresses": ["abdel@xerintel.es"],
    "on_failure": true,
    "on_timeout": true,
    "on_complete": false,
    "on_daemon_lifecycle": true
}
```

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

[Install]
WantedBy=multi-user.target
```

## Code Changes Summary

### New files

- None (all changes in existing modules)

### Modified files

| File | Changes |
|------|---------|
| `orchestrator.py` | Add `run_forever()`, add `_write_state()`, add `_interruptible_sleep()`, connect notification hooks |
| `cli.py` | Add `daemon` subcommand, update `status` to read state file |
| `config.py` | Add `LoopConfig` dataclass, parse `loop` section, add `state_file` to orchestrator, per-job timeout |
| `models.py` | Add `timeout` field to `Job`, update `JobRun.timeout()` docstring |
| `notifier.py` | Add `send_timeout_alert()`, `send_cycle_summary()`, `send_daemon_lifecycle()` methods |
| `pyproject.toml` | Version bump to 0.3.0, fix build-backend and license classifier |

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
7. Update `/etc/jetbackup-remote/config.json` (new jobs, kvm313, notification, loop section)
8. Create `/var/lib/jetbackup-remote/`
9. Remove timer unit, install new service unit
10. `systemctl daemon-reload && systemctl enable --now jetbackup-remote`
11. Verify daemon startup email received
12. Monitor first cycle via `jetbackup-remote status` and log

## Version

`0.2.0` → `0.3.0`
