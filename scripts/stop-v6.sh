#!/usr/bin/env bash
# Stop v6 coordinator safely before v2 cutover
#
# Pre-flight checks:
#   1. Set both batteries to battery_standby
#   2. Set EMS power limit = 0 + verify readback
#   3. Disable v6 custom component in HA
#
# Usage: ./scripts/stop-v6.sh
# Requires: HA_TOKEN set in environment or /etc/carma-box/env

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

ha_get_state() {
    local entity="$1"
    ha_api GET "states/${entity}" | python3 -c "import sys,json; print(json.load(sys.stdin).get('state','?'))" 2>/dev/null || echo "?"
}

echo "=== v6 Safe Shutdown ==="

# Pre-flight: verify HA is reachable
echo "Pre-flight: checking HA connectivity..."
HA_STATUS=$(curl -s -o /dev/null -w "%{http_code}" \
    -H "Authorization: Bearer ${HA_TOKEN}" \
    "${HA_URL}/api/" 2>/dev/null || echo "000")
if [ "${HA_STATUS}" != "200" ]; then
    echo "ABORT: HA not reachable at ${HA_URL} (HTTP ${HA_STATUS})"
    exit 1
fi
echo "  HA reachable (HTTP 200)"

# Pre-flight: save state snapshot for rollback reference
SNAPSHOT_FILE="/tmp/v6_shutdown_state.txt"
echo "Pre-flight: saving state snapshot to ${SNAPSHOT_FILE}..."
echo "v6 shutdown snapshot $(date -Iseconds)" > "${SNAPSHOT_FILE}"
for bat in kontor forrad; do
    mode=$(ha_get_state "select.goodwe_${bat}_ems_mode")
    limit=$(ha_get_state "number.goodwe_${bat}_ems_power_limit")
    soc=$(ha_get_state "sensor.goodwe_battery_state_of_charge_${bat}")
    echo "  ${bat}: mode=${mode}, limit=${limit}, soc=${soc}%"
    echo "${bat}: mode=${mode}, limit=${limit}, soc=${soc}%" >> "${SNAPSHOT_FILE}"
done

# 1. Set batteries to standby
echo "Step 1: Setting batteries to battery_standby..."
for bat in kontor forrad; do
    ha_api POST "services/goodwe/set_parameter" \
        "{\"entity_id\": \"select.goodwe_${bat}_ems_mode\", \"value\": \"battery_standby\"}" \
        > /dev/null 2>&1 || echo "  WARN: failed to set ${bat} standby"
done
sleep 5

# 2. Set EMS power limit = 0
echo "Step 2: Setting EMS power limits to 0..."
for bat in kontor forrad; do
    ha_api POST "services/goodwe/set_parameter" \
        "{\"entity_id\": \"number.goodwe_${bat}_ems_power_limit\", \"value\": 0}" \
        > /dev/null 2>&1 || echo "  WARN: failed to zero ${bat} limit"
done
sleep 5

# 3. Verify readback — mode AND power limit
echo "Step 3: Verifying readback..."
ERRORS=0
for bat in kontor forrad; do
    mode=$(ha_get_state "select.goodwe_${bat}_ems_mode")
    limit=$(ha_get_state "number.goodwe_${bat}_ems_power_limit")
    echo "  ${bat}: mode=${mode}, ems_power_limit=${limit}"

    if [ "${mode}" != "battery_standby" ]; then
        echo "  ERROR: ${bat} mode is '${mode}', expected 'battery_standby'"
        ERRORS=$((ERRORS + 1))
    fi
    if [ "${limit}" != "0" ] && [ "${limit}" != "0.0" ]; then
        echo "  ERROR: ${bat} ems_power_limit is '${limit}', expected 0"
        ERRORS=$((ERRORS + 1))
    fi

    # B7/INV-3: fast_charging must be OFF
    fc=$(ha_get_state "switch.goodwe_${bat}_fast_charging")
    if [ "${fc}" = "on" ]; then
        echo "  WARN: ${bat} fast_charging ON — forcing OFF"
        ha_api POST "services/switch/turn_off" \
            "{\"entity_id\": \"switch.goodwe_${bat}_fast_charging\"}" \
            > /dev/null 2>&1 || true
        sleep 2
        # Readback verify
        fc_after=$(ha_get_state "switch.goodwe_${bat}_fast_charging")
        if [ "${fc_after}" != "off" ]; then
            echo "  ERROR: ${bat} fast_charging still '${fc_after}' after force-off"
            ERRORS=$((ERRORS + 1))
        else
            echo "  OK: ${bat} fast_charging verified OFF"
        fi
    fi
done

if [ "${ERRORS}" -gt 0 ]; then
    echo ""
    echo "ABORT: ${ERRORS} verification failures. Fix manually before cutover."
    exit 1
fi
echo "  All verified OK."

# 4. Disable v6 custom component
echo "Step 4: Disabling v6 automations..."
ha_api POST "services/automation/turn_off" \
    '{"entity_id": "group.carma_box_v6_automations"}' \
    > /dev/null 2>&1 || echo "  WARN: v6 automation group not found (may need manual disable)"

echo ""
echo "=== v6 shutdown complete ==="
echo "Batteries in standby, limits verified at 0."
echo "Start v2: sudo systemctl start carma-box"
