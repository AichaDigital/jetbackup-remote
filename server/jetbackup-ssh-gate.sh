#!/bin/bash
# jetbackup-ssh-gate.sh - SSH command filter for jetbackup-remote
#
# Install in /usr/local/bin/jetbackup-ssh-gate.sh on each JetBackup server.
# Use with command= restriction in authorized_keys:
#
#   command="/usr/local/bin/jetbackup-ssh-gate.sh",no-port-forwarding,no-X11-forwarding,no-agent-forwarding,no-pty ssh-ed25519 AAAA... root@raspberrypinas
#
# Only allows specific jetbackup5api functions and echo (for connectivity test).
# All other commands are denied and logged.
#
# Copyright (C) 2026 Abdelkarim Mateos
# SPDX-License-Identifier: AGPL-3.0-or-later

set -euo pipefail

LOG_TAG="jetbackup-ssh-gate"
CMD="${SSH_ORIGINAL_COMMAND:-}"

if [ -z "$CMD" ]; then
    logger -t "$LOG_TAG" "DENIED: empty command from ${SSH_CLIENT%% *}"
    echo "ERROR: No command provided" >&2
    exit 1
fi

# Whitelist of allowed commands (exact prefix match)
ALLOWED_PATTERNS=(
    # Backup job functions
    'jetbackup5api -F getBackupJob '
    'jetbackup5api -F runBackupJobManually '
    'jetbackup5api -F editBackupJob '
    'jetbackup5api -F listBackupJobs'
    # Destination functions
    'jetbackup5api -F manageDestinationState '
    'jetbackup5api -F getDestination '
    'jetbackup5api -F listDestinations'
    # Queue functions
    'jetbackup5api -F listQueueGroups '
    'jetbackup5api -F getQueueGroup '
    'jetbackup5api -F listQueueItems '
    'jetbackup5api -F stopQueueGroup '
    'jetbackup5api -F clearQueue '
    # Log functions
    'jetbackup5api -F listLogs '
    'jetbackup5api -F getLog '
    'jetbackup5api -F listLogItems '
    'jetbackup5api -F getLogItem '
    # Connectivity test
    'echo '
)

for pattern in "${ALLOWED_PATTERNS[@]}"; do
    if [[ "$CMD" == ${pattern}* ]]; then
        logger -t "$LOG_TAG" "ALLOWED: $CMD from ${SSH_CLIENT%% *}"
        exec bash -c "$CMD"
    fi
done

# Command not in whitelist
logger -t "$LOG_TAG" "DENIED: $CMD from ${SSH_CLIENT%% *}"
echo "ERROR: Command not allowed: $CMD" >&2
exit 1
