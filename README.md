# jetbackup-remote

Remote orchestrator that serializes JetBackup5 backup jobs across multiple servers to prevent storage controller saturation.

## Problem

When multiple JetBackup5 servers push backups simultaneously to a NAS connected via USB (JMicron JMS567 RAID controller on Raspberry Pi), the controller saturates and throughput drops to ~1KB/s. This makes backups that should take 1-2 hours take days.

## Solution

`jetbackup-remote` runs on the Raspberry Pi and triggers backup jobs one at a time (FIFO queue), polling for completion before starting the next. The NAS only handles one write stream at a time, keeping throughput at full speed.

## Features

- **FIFO queue**: Jobs execute one at a time across all servers
- **SSH + jetbackup5api**: Triggers and monitors jobs remotely via the JetBackup5 CLI API
- **Command-restricted SSH**: Gate script limits Pi's access to only backup API functions
- **Non-aborting timeouts**: Timed-out jobs are reported, never killed (prevents backup corruption)
- **Email notifications**: Alerts on failure, timeout, or completion
- **Graceful shutdown**: Handles SIGTERM/SIGINT cleanly
- **Lock file**: Prevents concurrent executions
- **Zero dependencies**: Pure Python stdlib, no pip packages needed

## Quick start

```bash
# Clone
git clone https://github.com/AichaDigital/jetbackup-remote.git
cd jetbackup-remote

# Configure
cp config.example.json /etc/jetbackup-remote/config.json
# Edit config.json with your servers and job IDs

# Test connectivity
python3 -m jetbackup_remote -c /etc/jetbackup-remote/config.json test

# Dry run
python3 -m jetbackup_remote -c /etc/jetbackup-remote/config.json run --dry-run

# Live run
python3 -m jetbackup_remote -c /etc/jetbackup-remote/config.json run
```

## CLI

```
jetbackup-remote run [--dry-run] [--server NAME] [--job ID]
jetbackup-remote status [--server NAME] [--json]
jetbackup-remote test [--server NAME]
jetbackup-remote list [--json]
jetbackup-remote stop --server NAME JOB_ID
jetbackup-remote validate
```

## Documentation

- [Manual de uso (español)](docs/MANUAL_ES.md) — Guía completa en español
- [Installation guide](docs/INSTALL.md)
- [Security model](docs/SECURITY.md)

## Requirements

- Python 3.9+
- SSH access to JetBackup5 servers
- JetBackup5 with `jetbackup5api` CLI

## License

[GNU Affero General Public License v3.0](https://www.gnu.org/licenses/agpl-3.0.html) — see [LICENSE](LICENSE)

## Author

Abdelkarim Mateos (abdelkarim@aichadigital.es)
