#!/usr/bin/env bash
# CARMA Box v2 — Deploy script
# Usage: ./scripts/deploy.sh [--restart]
#
# Deploys carma-box-v2 to /opt/carma-box:
#   1. Create user/dirs if missing
#   2. Sync code from repo
#   3. Install/update venv
#   4. Copy config + systemd unit
#   5. Reload/restart service
#
# Exit codes: 0=OK, 1=error

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
INSTALL_DIR="/opt/carma-box"
CONFIG_DIR="/etc/carma-box"
DATA_DIR="/var/lib/carma-box"
LOG_DIR="/var/log/carma-box"
SERVICE_NAME="carma-box"
VENV_DIR="${INSTALL_DIR}/venv"

RESTART="${1:-}"

echo "=== CARMA Box v2 Deploy ==="
echo "Repo:    ${REPO_DIR}"
echo "Install: ${INSTALL_DIR}"
echo ""

# 1. Create user if missing
if ! id -u carma-box &>/dev/null; then
    echo "Creating user carma-box..."
    sudo useradd --system --no-create-home --shell /usr/sbin/nologin carma-box
fi

# 2. Create directories
for dir in "${INSTALL_DIR}" "${CONFIG_DIR}" "${DATA_DIR}" "${LOG_DIR}"; do
    if [ ! -d "$dir" ]; then
        echo "Creating ${dir}..."
        sudo mkdir -p "$dir"
    fi
    sudo chown carma-box:carma-box "$dir"
done

# 3. Sync code (exclude .git, tests, docs)
echo "Syncing code to ${INSTALL_DIR}..."
sudo rsync -a --delete \
    --exclude='.git' \
    --exclude='tests/' \
    --exclude='docs/' \
    --exclude='__pycache__' \
    --exclude='.mypy_cache' \
    --exclude='.pytest_cache' \
    --exclude='.ruff_cache' \
    --exclude='venv/' \
    "${REPO_DIR}/" "${INSTALL_DIR}/"
sudo chown -R carma-box:carma-box "${INSTALL_DIR}"

# 4. Create/update venv
if [ ! -d "${VENV_DIR}" ]; then
    echo "Creating venv..."
    sudo -u carma-box python3 -m venv "${VENV_DIR}"
fi

echo "Installing dependencies..."
sudo -u carma-box "${VENV_DIR}/bin/pip" install --quiet --upgrade pip
if [ -f "${INSTALL_DIR}/requirements.txt" ]; then
    sudo -u carma-box "${VENV_DIR}/bin/pip" install --quiet -r "${INSTALL_DIR}/requirements.txt"
fi

# 5. Copy config if not exists (don't overwrite live config)
if [ ! -f "${CONFIG_DIR}/site.yaml" ]; then
    echo "Copying initial config..."
    sudo cp "${INSTALL_DIR}/config/site.yaml" "${CONFIG_DIR}/site.yaml"
    sudo chown carma-box:carma-box "${CONFIG_DIR}/site.yaml"
else
    echo "Config exists — not overwriting ${CONFIG_DIR}/site.yaml"
fi

# 6. Create EnvironmentFile if missing
if [ ! -f "${CONFIG_DIR}/env" ]; then
    echo "Creating EnvironmentFile..."
    sudo tee "${CONFIG_DIR}/env" > /dev/null << 'ENVEOF'
# CARMA Box environment
# HA_TOKEN is read from 1Password at runtime or set here
# HA_TOKEN=
HA_URL=http://192.168.5.22:8123
ENVEOF
    sudo chown carma-box:carma-box "${CONFIG_DIR}/env"
    sudo chmod 600 "${CONFIG_DIR}/env"
fi

# 7. Install systemd unit
echo "Installing systemd unit..."
sudo cp "${INSTALL_DIR}/carma-box.service" /etc/systemd/system/carma-box.service
sudo systemctl daemon-reload

# 8. Enable service
sudo systemctl enable "${SERVICE_NAME}" --quiet 2>/dev/null || true

# 9. Restart or reload
if [ "${RESTART}" = "--restart" ]; then
    echo "Restarting ${SERVICE_NAME}..."
    sudo systemctl restart "${SERVICE_NAME}"
else
    echo "Service installed. Start with: sudo systemctl start ${SERVICE_NAME}"
fi

# 10. Status
echo ""
echo "=== Deploy complete ==="
sudo systemctl status "${SERVICE_NAME}" --no-pager -l 2>/dev/null || echo "(service not started)"
echo ""
echo "Logs: sudo journalctl -u ${SERVICE_NAME} -f"
