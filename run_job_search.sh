#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="${SCRIPT_DIR}/cron_job_search.log"
SUMMARY_FILE="${SCRIPT_DIR}/latest_job_summary.md"

# cron can start with a minimal environment
export HOME="${HOME:-$(getent passwd "$(id -u)" | cut -d: -f6)}"
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:${HOME}/.local/bin"
export PYTHONUNBUFFERED=1

# Load ~/.config/notion/job-search.env, then a local .env (whose values win) when present
if [ -f "${HOME}/.config/notion/job-search.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "${HOME}/.config/notion/job-search.env"
  set +a
fi
if [ -f "${SCRIPT_DIR}/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "${SCRIPT_DIR}/.env"
  set +a
fi

# Prevent overlapping manual and scheduled runs from clobbering shared JSON files
already_running() {
  echo "Job search already running; skipping ($(date '+%Y-%m-%d %H:%M:%S'))" | tee -a "$LOG_FILE"
  exit 0
}
if command -v flock >/dev/null 2>&1; then
  exec 9>"${SCRIPT_DIR}/.run_job_search.lock"
  flock -n 9 || already_running
else
  # macOS has no flock: use an atomic mkdir lock holding the owner's PID, and take over a stale one
  LOCK_DIR="${SCRIPT_DIR}/.run_job_search.lock.d"
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

echo "==========================================" | tee -a "$LOG_FILE"
echo "Job Search Pipeline Started: $(date '+%Y-%m-%d %H:%M:%S')" | tee -a "$LOG_FILE"
echo "==========================================" | tee -a "$LOG_FILE"

cd "$SCRIPT_DIR"

# Use the virtualenv from the README's install steps when present; cron does not activate it
PYTHON="${SCRIPT_DIR}/.venv/bin/python"
[ -x "$PYTHON" ] || PYTHON="python3"

EXIT_CODE=0
if [ $# -gt 0 ]; then
  "$PYTHON" "${SCRIPT_DIR}/run_pipeline.py" "$@" 2>&1 | tee -a "$LOG_FILE" || EXIT_CODE=$?
else
  "$PYTHON" "${SCRIPT_DIR}/run_pipeline.py" \
    --md-output "$SUMMARY_FILE" 2>&1 | tee -a "$LOG_FILE" || EXIT_CODE=$?
fi

echo "Finished with exit code ${EXIT_CODE} at $(date '+%Y-%m-%d %H:%M:%S')" | tee -a "$LOG_FILE"
exit $EXIT_CODE
