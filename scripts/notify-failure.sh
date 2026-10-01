#!/usr/bin/env bash
# scripts/notify-failure.sh — push a phone alert when a scheduled run fails
#
# Run by morning-brief-failure@.service (OnFailure= of morning-brief.service).
# Sends the tail of the last run's log through hub's publisher. Exits non-zero
# if the alert could not be sent, so a silent failure shows up in systemd.
#
# Usage:
#   bash scripts/notify-failure.sh <failed-unit>    # e.g. morning-brief.service
#
# Environment:
#   HUB_ALERT   publisher (default /usr/local/sbin/hub-alert)
#   MB_RUN_LOG  run log (default $XDG_STATE_HOME/morning-brief-last-run.log)

set -euo pipefail

UNIT="${1:?usage: notify-failure.sh <failed-unit>}"
HUB_ALERT="${HUB_ALERT:-/usr/local/sbin/hub-alert}"
LOG="${MB_RUN_LOG:-${XDG_STATE_HOME:-$HOME/.local/state}/morning-brief-last-run.log}"
MAX_BYTES=3500 # ntfy message limit is 4096

if [[ ! -x "${HUB_ALERT}" ]]; then
    echo "notify-failure: publisher missing or not executable: ${HUB_ALERT}" >&2
    exit 1
fi

footer="Full log: cat ${LOG}"
if [[ -f "${LOG}" ]]; then
    tail=$(tail -n 15 "${LOG}")
else
    tail="(no run log at ${LOG})"
fi

# Trim from the top until the body fits
while (($(printf '%s\n%s' "${tail}" "${footer}" | wc -c) > MAX_BYTES)) && [[ -n "${tail}" ]]; do
    if [[ "${tail}" == *$'\n'* ]]; then
        tail="${tail#*$'\n'}"
    else
        tail="${tail: -$((MAX_BYTES / 2))}"
        break
    fi
done
body="${tail}"$'\n'"${footer}"

if ! "${HUB_ALERT}" "morning-brief failed: ${UNIT}" "${body}" high; then
    echo "notify-failure: ${HUB_ALERT} exited non-zero" >&2
    exit 1
fi
