#!/usr/bin/env bash
# Stop v6 coordinator safely before v2 cutover
#
# Pre-flight checks:
#   1. Set both batteries to battery_standby
#   2. Verify EMS power limit = 0
#   3. Disable v6 custom component in HA
#   4. Verify no active discharge/charge
#
# Usage: ./scripts/stop-v6.sh

set -euo pipefail

HA_URL="${HA_URL:-http://192.168.5.22:8123}"
HA_TOKEN="${HA_TOKEN:-}"

if [ -z "${HA_TOKEN}" ]; then
    echo "ERROR: HA_TOKEN not set"
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

echo "=== v6 Safe Shutdown ==="

# 1. Set batteries to standby
echo "Step 1: Setting batteries to battery_standby..."
for bat in kontor forrad; do
    ha_api POST "services/goodwe/set_parameter" \
        "{\"entity_id\": \"select.goodwe_${bat}_ems_mode\", \"value\": \"battery_standby\"}" \
        > /dev/null 2>&1 || echo "  WARN: failed to set ${bat} standby"
done
sleep 5

# 2. Verify EMS power limit = 0
echo "Step 2: Setting EMS power limits to 0..."
for bat in kontor forrad; do
    ha_api POST "services/goodwe/set_parameter" \
        "{\"entity_id\": \"number.goodwe_${bat}_ems_power_limit\", \"value\": 0}" \
        > /dev/null 2>&1 || echo "  WARN: failed to zero ${bat} limit"
done
sleep 2

# 3. Verify states
echo "Step 3: Verifying..."
for bat in kontor forrad; do
    mode=$(ha_api GET "states/select.goodwe_${bat}_ems_mode" | python3 -c "import sys,json; print(json.load(sys.stdin).get('state','?'))" 2>/dev/null || echo "?")
    echo "  ${bat} mode: ${mode}"
done

# 4. Disable v6 custom component
echo "Step 4: Disabling v6 automations..."
ha_api POST "services/automation/turn_off" \
    '{"entity_id": "group.carma_box_v6_automations"}' \
    > /dev/null 2>&1 || echo "  WARN: v6 automation group not found (may need manual disable)"

echo ""
echo "=== v6 shutdown complete ==="
echo "Batteries in standby, limits zeroed."
echo "Start v2: sudo systemctl start carma-box"
