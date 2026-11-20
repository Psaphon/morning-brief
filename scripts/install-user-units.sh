#!/usr/bin/env bash
# scripts/install-user-units.sh — set up the scheduled pipeline for this user
#
# Creates the dedicated pipeline clone that morning-brief.service runs from and
# links the service, timer and failure-alert template into the user's systemd.
# Idempotent. Does NOT enable the timer; it prints that command instead.
#
# Usage:
#   bash scripts/install-user-units.sh [branch]    # branch defaults to main (released code)
#
# One-time, by hand: put the keys file (.env) and any existing data/ history
# into the pipeline clone. Neither goes through git.

set -euo pipefail

BRANCH="${1:-main}"
PIPELINE="${HOME}/.local/share/morning-brief-pipeline"
REPO_URL="$(git -C "$(dirname "$0")/.." config --get remote.origin.url)"

if [[ ! -d "${PIPELINE}/.git" ]]; then
    git clone --branch "${BRANCH}" "${REPO_URL}" "${PIPELINE}"
else
    echo "Pipeline clone exists: ${PIPELINE} ($(git -C "${PIPELINE}" branch --show-current))"
fi

# The unit's run log lives directly in here, and systemd won't create it
mkdir -p "${XDG_STATE_HOME:-${HOME}/.local/state}"

systemctl --user link "${PIPELINE}/morning-brief.service" "${PIPELINE}/morning-brief.timer" \
    "${PIPELINE}/morning-brief-failure@.service"
systemctl --user daemon-reload

[[ -f "${PIPELINE}/.env" ]] || echo "WARNING: no ${PIPELINE}/.env yet (API keys); the pipeline needs it."

echo "Installed. Run once now:   systemctl --user start morning-brief.service"
echo "Turn on the daily timer:  systemctl --user enable --now morning-brief.timer"
