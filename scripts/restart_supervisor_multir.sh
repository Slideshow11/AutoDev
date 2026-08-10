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

# 1. Find any running supervisor.
PID=$(pgrep -f "$SUP_DIR/supervisor.py" || true)
if [ -n "$PID" ]; then
    echo "Stopping supervisor PID $PID"
    kill -TERM "$PID" 2>/dev/null || true
    # Wait for the lock file to be released.
    for _ in $(seq 1 30); do
        if [ ! -f "$LOCK" ]; then
            break
        fi
        sleep 1
    done
fi

# 2. Start a new supervisor with multi-PR.
echo "Starting supervisor with AED_PR_NUMBERS=${AED_PR_NUMBERS:-4,5}"
cd "$SUP_DIR"
nohup python3 "$SUP_DIR/supervisor.py" > "$SUP_DIR/logs/supervisor.out" 2>&1 &
NEW_PID=$!
echo "New supervisor PID $NEW_PID"
sleep 2
cat "$HEARTBEAT"
