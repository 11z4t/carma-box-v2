#!/usr/bin/env bash
# Rollback from v2 to v6
#
# 1. Stop carma-box v2 service
# 2. Set batteries to safe standby
# 3. Re-enable v6 automations
#
# Usage: ./scripts/rollback.sh

set -euo pipefail

# Source env file if it exists (before defaults)
ENV_FILE="/etc/carma-box/env"
if [ -f "${ENV_FILE}" ]; then
    # shellcheck source=/dev/null
    source "${ENV_FILE}"
fi

HA_URL="${HA_URL:-http://192.168.5.22:8123}"
HA_TOKEN="${HA_TOKEN:-}"

if [ -z "${HA_TOKEN}" ]; then
    echo "ERROR: HA_TOKEN not set (set in env or ${ENV_FILE})"
    exit 1
fi

ha_api() {
    local method="$1"
    local path="$2"
    local data="${3:-}"

    if [ -n "$data" ]; then
        curl -s -X "${method}" \
            -H "Authorization: Bearer ${HA_TOKEN}" \
            -H "Content-Type: application/json" \
            -d "${data}" \
            "${HA_URL}/api/${path}"
    else
        curl -s -X "${method}" \
            -H "Authorization: Bearer ${HA_TOKEN}" \
            "${HA_URL}/api/${path}"
    fi
}

echo "=== Rollback: v2 → v6 ==="

# 1. Stop v2
echo "Step 1: Stopping carma-box v2..."
sudo systemctl stop carma-box 2>/dev/null || echo "  (service not running)"

# 2. Safe state
echo "Step 2: Setting batteries to standby..."
for bat in kontor forrad; do
    ha_api POST "services/goodwe/set_parameter" \
        "{\"entity_id\": \"select.goodwe_${bat}_ems_mode\", \"value\": \"battery_standby\"}" \
        > /dev/null 2>&1 || echo "  WARN: failed to set ${bat}"
    ha_api POST "services/goodwe/set_parameter" \
        "{\"entity_id\": \"number.goodwe_${bat}_ems_power_limit\", \"value\": 0}" \
        > /dev/null 2>&1 || true
done

# 3. Re-enable v6
echo "Step 3: Re-enabling v6 automations..."
ha_api POST "services/automation/turn_on" \
    '{"entity_id": "group.carma_box_v6_automations"}' \
    > /dev/null 2>&1 || echo "  WARN: v6 automation group not found"

echo ""
echo "=== Rollback complete ==="
echo "v2 stopped, batteries in standby, v6 automations re-enabled."
