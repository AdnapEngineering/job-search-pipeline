#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="${SCRIPT_DIR}/data"
LOG_FILE="${DATA_DIR}/cron_job_search.log"
SUMMARY_FILE="${DATA_DIR}/latest_job_summary.md"
mkdir -p "$DATA_DIR"

# cron can start with a minimal environment
export HOME="${HOME:-$(getent passwd "$(id -u)" | cut -d: -f6)}"
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:${HOME}/.local/bin"
export PYTHONUNBUFFERED=1

# Prevent overlapping manual and scheduled runs from clobbering shared JSON files
already_running() {
  echo "Job search already running; skipping ($(date '+%Y-%m-%d %H:%M:%S'))" | tee -a "$LOG_FILE"
  exit 0
}
if command -v flock >/dev/null 2>&1; then
  exec 9>"${DATA_DIR}/.run_job_search.lock"
  flock -n 9 || already_running
else
  # macOS has no flock: use an atomic mkdir lock holding the owner's PID, and take over a stale one
  LOCK_DIR="${DATA_DIR}/.run_job_search.lock.d"
  if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    OWNER="$(cat "${LOCK_DIR}/pid" 2>/dev/null || true)"
    if [ -n "$OWNER" ] && kill -0 "$OWNER" 2>/dev/null; then
      already_running
    fi
    rm -rf "$LOCK_DIR"
    mkdir "$LOCK_DIR" 2>/dev/null || already_running
  fi
  echo $$ > "${LOCK_DIR}/pid"
  trap 'rm -rf "$LOCK_DIR"' EXIT
fi

# Keep the log bounded: past JOB_SEARCH_LOG_MAX_BYTES (default 5 MB), keep its newest half, from a line start
LOG_MAX_BYTES="${JOB_SEARCH_LOG_MAX_BYTES:-5242880}"
if [ "$LOG_MAX_BYTES" -gt 0 ] && [ -f "$LOG_FILE" ] && [ "$(( $(wc -c < "$LOG_FILE") ))" -gt "$LOG_MAX_BYTES" ]; then
  tail -c "$(( LOG_MAX_BYTES / 2 ))" "$LOG_FILE" | tail -n +2 > "${LOG_FILE}.tmp" && mv "${LOG_FILE}.tmp" "$LOG_FILE"
fi

echo "==========================================" | tee -a "$LOG_FILE"
echo "Job Search Pipeline Started: $(date '+%Y-%m-%d %H:%M:%S')" | tee -a "$LOG_FILE"
echo "==========================================" | tee -a "$LOG_FILE"

cd "$SCRIPT_DIR"

# Use the virtualenv from the README's install steps when present; cron does not activate it
PYTHON="${SCRIPT_DIR}/.venv/bin/python"
[ -x "$PYTHON" ] || PYTHON="python3"

EXIT_CODE=0
if [ $# -gt 0 ]; then
  "$PYTHON" -m job_search_pipeline "$@" 2>&1 | tee -a "$LOG_FILE" || EXIT_CODE=$?
else
  "$PYTHON" -m job_search_pipeline \
    --md-output "$SUMMARY_FILE" 2>&1 | tee -a "$LOG_FILE" || EXIT_CODE=$?
fi

echo "Finished with exit code ${EXIT_CODE} at $(date '+%Y-%m-%d %H:%M:%S')" | tee -a "$LOG_FILE"
exit $EXIT_CODE
