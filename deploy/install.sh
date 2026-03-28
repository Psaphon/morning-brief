#!/usr/bin/env bash
# deploy/install.sh — install Morning Brief systemd timer on the host
#
# Usage:
#   sudo bash deploy/install.sh [--install-dir /opt/morning-brief]
#
# What it does:
#   1. Copies the repo to INSTALL_DIR (default /opt/morning-brief)
#   2. Installs the systemd service and timer units
#   3. Enables and starts the timer
#   4. Optionally pulls the Ollama model (pass --pull-model)

set -euo pipefail

INSTALL_DIR="/opt/morning-brief"
PULL_MODEL=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --install-dir) INSTALL_DIR="$2"; shift 2 ;;
        --pull-model)  PULL_MODEL=true; shift ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

if [[ $EUID -ne 0 ]]; then
    echo "Error: this script must be run as root (use sudo)." >&2
    exit 1
fi

echo "==> Installing Morning Brief to ${INSTALL_DIR}"

# Copy repo files (preserve existing data/ volume)
mkdir -p "${INSTALL_DIR}"
rsync -av --exclude='.git' --exclude='data/' . "${INSTALL_DIR}/"

# Ensure data directories exist and are writable
mkdir -p "${INSTALL_DIR}/data/output" "${INSTALL_DIR}/data/logs"

# Copy .env if not already present
if [[ ! -f "${INSTALL_DIR}/.env" ]]; then
    if [[ -f ".env" ]]; then
        cp .env "${INSTALL_DIR}/.env"
        echo "    Copied .env to ${INSTALL_DIR}/.env"
    else
        cp .env.example "${INSTALL_DIR}/.env"
        echo "    WARNING: No .env found — copied .env.example. Fill in API keys before running."
    fi
fi

echo "==> Installing systemd units"

# Patch WorkingDirectory in the service file to the actual install dir
sed "s|WorkingDirectory=.*|WorkingDirectory=${INSTALL_DIR}|" \
    "${INSTALL_DIR}/deploy/morning-brief.service" \
    > /etc/systemd/system/morning-brief.service

# Patch log paths in the service file
sed -i "s|/opt/morning-brief/data/logs|${INSTALL_DIR}/data/logs|g" \
    /etc/systemd/system/morning-brief.service

cp "${INSTALL_DIR}/deploy/morning-brief.timer" /etc/systemd/system/morning-brief.timer

systemctl daemon-reload
systemctl enable morning-brief.timer
systemctl start morning-brief.timer

echo "==> Timer installed and enabled"
systemctl status morning-brief.timer --no-pager

if [[ "${PULL_MODEL}" == true ]]; then
    echo "==> Starting Ollama and pulling model (this may take a while)..."
    cd "${INSTALL_DIR}"
    docker compose up -d ollama
    bash scripts/pull-model.sh
fi

echo ""
echo "Done! Next run: $(systemctl show morning-brief.timer -p NextElapseUSecRealtime --value)"
echo ""
echo "Useful commands:"
echo "  Check timer:          systemctl status morning-brief.timer"
echo "  View logs:            journalctl -u morning-brief.service -f"
echo "  Run pipeline now:     cd ${INSTALL_DIR} && docker compose run --rm morning-brief"
echo "  Check last run:       cat ${INSTALL_DIR}/data/last_run.json"
