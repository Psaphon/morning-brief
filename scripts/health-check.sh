#!/usr/bin/env bash
# scripts/health-check.sh — post-run validation for the Morning Brief pipeline
#
# Called by the systemd service unit (ExecStartPost) after the pipeline
# completes.  Verifies the dashboard was generated within the last hour and
# logs any failure to ~/.local/share/morning-brief/failures.log.
#
# Exit codes:
#   0  — dashboard exists and was modified within the last hour
#   1  — dashboard missing or stale
#
# Usage:
#   bash scripts/health-check.sh [data-dir]   # data-dir defaults to ./data

set -euo pipefail

DATA_DIR="${1:-data}"
DASHBOARD="${DATA_DIR}/output/dashboard.html"
MAX_AGE_SECONDS=3600  # 1 hour
FAILURE_LOG="${HOME}/.local/share/morning-brief/failures.log"
TIMESTAMP="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

_log_failure() {
    local message="$1"
    local exit_code="${2:-1}"
    mkdir -p "$(dirname "${FAILURE_LOG}")"
    printf '%s  exit=%d  %s\n' "${TIMESTAMP}" "${exit_code}" "${message}" >> "${FAILURE_LOG}"
    echo "[FAIL] ${message}" >&2
    exit "${exit_code}"
}

# ── 1. Dashboard must exist ───────────────────────────────────────────────────
if [[ ! -f "${DASHBOARD}" ]]; then
    _log_failure "dashboard not found: ${DASHBOARD}" 1
fi

# ── 2. Dashboard must be recent (modified within the last hour) ───────────────
age=$(python3 -c "import os, time; print(int(time.time() - os.path.getmtime('${DASHBOARD}')))")
if (( age > MAX_AGE_SECONDS )); then
    minutes=$(( age / 60 ))
    _log_failure "dashboard is stale (${minutes}m old, limit is 60m): ${DASHBOARD}" 1
fi

minutes=$(( age / 60 ))
seconds=$(( age % 60 ))
echo "[OK]  Dashboard is current (generated ${minutes}m ${seconds}s ago)"
