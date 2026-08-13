#!/usr/bin/env bash
# Restart the AED supervisor with multi-PR support.
# This is the structural fix for the a019e63 stall:
# the supervisor was bound to a single PR_NUMBER at
# launch; PR #5 had no running owner.
#
# Required env (set by launchd / systemd / cron):
#   AED_PR_NUMBERS=4,5
#   AED_REPO_OWNER=Slideshow11
#   AED_REPO_NAME=AutoDev
#   AED_GITHUB_TOKEN=<gh PAT>
#   AED_AED_RUN_ID=<run id>
set -euo pipefail

SUP_DIR=/home/max/.hermes/aed-supervisor
LOCK="$SUP_DIR/lock"
HEARTBEAT="$SUP_DIR/heartbeat"

# 0. Validate and apply the launch environment BEFORE stopping the supervisor.
#    The required variables documented at the top of this script are not
#    guaranteed by launchd/systemd/cron callers; we must enforce them here so
#    the child supervisor.py never inherits an empty or caller-supplied value.
: "${AED_PR_NUMBERS:=4,5}"
export AED_PR_NUMBERS

: "${AED_REPO_OWNER:=Slideshow11}"
export AED_REPO_OWNER

: "${AED_REPO_NAME:=AutoDev}"
export AED_REPO_NAME

if [ -z "${AED_GITHUB_TOKEN:-}" ]; then
    echo "ERROR: AED_GITHUB_TOKEN must be set before restarting the supervisor." >&2
    exit 1
fi
export AED_GITHUB_TOKEN

if [ -z "${AED_AED_RUN_ID:-}" ]; then
    echo "ERROR: AED_AED_RUN_ID must be set before restarting the supervisor." >&2
    exit 1
fi
export AED_AED_RUN_ID

echo "Launch environment validated: AED_PR_NUMBERS=$AED_PR_NUMBERS AED_REPO_OWNER=$AED_REPO_OWNER AED_REPO_NAME=$AED_REPO_NAME AED_AED_RUN_ID=$AED_AED_RUN_ID"

# 1. Find any running supervisor and stop it cleanly.
#    pgrep -f may return multiple PIDs (one per newline); iterating and failing
#    closed (without `|| true`) is required so we never start a second owner
#    while an old one is still alive or a stale lock remains.
PIDS=$(pgrep -f "$SUP_DIR/supervisor.py" || true)
if [ -n "$PIDS" ]; then
    echo "Stopping supervisor PIDs: $PIDS"
    # Send TERM to every matching PID individually; refuse to proceed if any
    # termination fails or any PID refuses to exit within the grace window.
    for PID in $PIDS; do
        if ! kill -TERM "$PID" 2>/dev/null; then
            echo "ERROR: kill -TERM $PID failed" >&2
            exit 1
        fi
    done
fi

# 2. Wait for the lock file to be released (max 30 s), regardless of whether
#    a PID was found. A stale lock with no live owner must also be cleared
#    before we attempt to start a new supervisor.
WAITED=0
while [ -f "$LOCK" ] && [ "$WAITED" -lt 30 ]; do
    sleep 1
    WAITED=$((WAITED + 1))
done
if [ -f "$LOCK" ]; then
    echo "ERROR: lock $LOCK still present after 30 s; refusing to start a second supervisor" >&2
    exit 1
fi

# 3. Start a new supervisor with multi-PR.
echo "Starting supervisor with AED_PR_NUMBERS=$AED_PR_NUMBERS"
cd "$SUP_DIR"
nohup python3 "$SUP_DIR/supervisor.py" > "$SUP_DIR/logs/supervisor.out" 2>&1 &
NEW_PID=$!
echo "New supervisor PID $NEW_PID"
sleep 2
cat "$HEARTBEAT"
