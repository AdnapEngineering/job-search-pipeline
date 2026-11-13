#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="${SCRIPT_DIR}/cron_job_search.log"
SUMMARY_FILE="${SCRIPT_DIR}/latest_job_summary.md"

export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:${HOME}/.local/bin"
export PYTHONUNBUFFERED=1

# Load environment variables from local .env or ~/.config/notion/job-search.env if present
if [ -f "${SCRIPT_DIR}/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "${SCRIPT_DIR}/.env"
  set +a
elif [ -f "${HOME}/.config/notion/job-search.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "${HOME}/.config/notion/job-search.env"
  set +a
fi

# Prevent overlapping manual and scheduled runs from clobbering shared JSON files
exec 9>"${SCRIPT_DIR}/.run_job_search.lock"
if ! flock -n 9; then
  echo "Job search already running; skipping ($(date '+%Y-%m-%d %H:%M:%S'))" | tee -a "$LOG_FILE"
  exit 0
fi

echo "==========================================" | tee -a "$LOG_FILE"
echo "Job Search Pipeline Started: $(date '+%Y-%m-%d %H:%M:%S')" | tee -a "$LOG_FILE"
echo "==========================================" | tee -a "$LOG_FILE"

cd "$SCRIPT_DIR"

EXIT_CODE=0
if [ $# -gt 0 ]; then
  python3 "${SCRIPT_DIR}/run_pipeline.py" "$@" 2>&1 | tee -a "$LOG_FILE" || EXIT_CODE=$?
else
  python3 "${SCRIPT_DIR}/run_pipeline.py" \
    --md-output "$SUMMARY_FILE" 2>&1 | tee -a "$LOG_FILE" || EXIT_CODE=$?
fi

echo "Finished with exit code ${EXIT_CODE} at $(date '+%Y-%m-%d %H:%M:%S')" | tee -a "$LOG_FILE"
exit $EXIT_CODE
