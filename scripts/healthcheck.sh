#!/usr/bin/env bash
# scripts/healthcheck.sh — verify the Morning Brief pipeline ran successfully
#
# Exit codes:
#   0  — dashboard generated and recent (< 25 hours old)
#   1  — dashboard missing or stale
#   2  — pipeline reported an error in last_run.json
#
# Usage:
#   bash scripts/healthcheck.sh               # uses default data/ path
#   bash scripts/healthcheck.sh /opt/morning-brief/data

set -euo pipefail

DATA_DIR="${1:-data}"
DASHBOARD="${DATA_DIR}/output/dashboard.html"
STATUS_FILE="${DATA_DIR}/last_run.json"
MAX_AGE_SECONDS=90000  # 25 hours

_fail() {
    echo "[FAIL] $*" >&2
    exit "${2:-1}"
}

# ── 1. Check status file ──────────────────────────────────────────────────────
if [[ -f "${STATUS_FILE}" ]]; then
    status=$(python3 -c "import json; d=json.load(open('${STATUS_FILE}')); print(d.get('status','unknown'))" 2>/dev/null || echo "unreadable")
    message=$(python3 -c "import json; d=json.load(open('${STATUS_FILE}')); print(d.get('message',''))" 2>/dev/null || echo "")
    timestamp=$(python3 -c "import json; d=json.load(open('${STATUS_FILE}')); print(d.get('timestamp',''))" 2>/dev/null || echo "")

    echo "[INFO] Last run: ${timestamp}"
    echo "[INFO] Status:   ${status}"
    echo "[INFO] Message:  ${message}"

    if [[ "${status}" == "error" ]]; then
        _fail "Pipeline reported an error: ${message}" 2
    fi
else
    echo "[WARN] No status file found at ${STATUS_FILE} — pipeline may not have run yet"
fi

# ── 2. Check dashboard file ───────────────────────────────────────────────────
if [[ ! -f "${DASHBOARD}" ]]; then
    _fail "Dashboard not found: ${DASHBOARD}"
fi

age=$(python3 -c "import os, time; print(int(time.time() - os.path.getmtime('${DASHBOARD}')))")
if (( age > MAX_AGE_SECONDS )); then
    hours=$(( age / 3600 ))
    _fail "Dashboard is stale (${hours}h old, limit is 25h): ${DASHBOARD}"
fi

hours=$(( age / 3600 ))
minutes=$(( (age % 3600) / 60 ))
echo "[OK]  Dashboard is current (generated ${hours}h ${minutes}m ago)"
