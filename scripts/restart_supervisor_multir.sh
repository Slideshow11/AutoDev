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
echo "Starting supervisor with AED_PR_NUMBERS=$AED_PR_NUMBERS"
cd "$SUP_DIR"
nohup python3 "$SUP_DIR/supervisor.py" > "$SUP_DIR/logs/supervisor.out" 2>&1 &
NEW_PID=$!
echo "New supervisor PID $NEW_PID"
sleep 2
cat "$HEARTBEAT"
