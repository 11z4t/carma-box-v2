#!/usr/bin/env bash
# CARMA Box — Home Assistant Supervisor Addon entrypoint
# PLAT-1813: HA-addon packaging
#
# Responsibilities:
#   1. Read addon options from /data/options.json (written by HA Supervisor)
#   2. Export secrets as environment variables (CARMA_HA_TOKEN etc.)
#   3. Validate that site.yaml exists at /homeassistant/carmabox/site.yaml
#   4. Patch log path + sqlite path in environment to point at /data/
#   5. Exec main.py (replaces this process — signals propagate correctly)
#
# Directory layout inside the addon container:
#   /app/                       ← carma-box-v2 application code (read-only image layer)
#   /data/                      ← HA Supervisor persistent volume (survives updates)
#     options.json              ← Written by Supervisor from addon UI options
#     carma.db                  ← SQLite state database
#     logs/
#       carma.log               ← Application log (rotated)
#   /homeassistant/             ← HA config dir (mapped read-write)
#     carmabox/
#       site.yaml               ← REQUIRED: full site configuration (user-managed)
#
# Signals:
#   SIGTERM → forwarded to python process → graceful shutdown in main.py

set -euo pipefail

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

readonly SITE_YAML="/homeassistant/carmabox/site.yaml"
readonly OPTIONS_JSON="/data/options.json"
readonly DB_PATH="/data/carma.db"
readonly LOG_DIR="/data/logs"
readonly LOG_FILE="${LOG_DIR}/carma.log"
readonly APP_DIR="/app"

# ---------------------------------------------------------------------------
# Logging helpers (before Python starts)
# ---------------------------------------------------------------------------

log_info()  { echo "[INFO]  [run.sh] $*"; }
log_warn()  { echo "[WARN]  [run.sh] $*" >&2; }
log_error() { echo "[ERROR] [run.sh] $*" >&2; }

# ---------------------------------------------------------------------------
# 1. Validate options.json exists
# ---------------------------------------------------------------------------

if [[ ! -f "${OPTIONS_JSON}" ]]; then
    log_error "Missing ${OPTIONS_JSON} — HA Supervisor should create this file."
    log_error "Is the addon running under HA Supervisor? Exiting."
    exit 1
fi

# ---------------------------------------------------------------------------
# 2. Parse options.json and export environment variables
# ---------------------------------------------------------------------------

log_info "Reading addon options from ${OPTIONS_JSON}"

# jq is available in the base image (installed in Dockerfile).
HA_TOKEN=$(jq -r '.ha_token // ""' "${OPTIONS_JSON}")
SOLCAST_KEY=$(jq -r '.solcast_api_key // ""' "${OPTIONS_JSON}")
NORDPOOL_KEY=$(jq -r '.nordpool_api_key // ""' "${OPTIONS_JSON}")
SLACK_WEBHOOK=$(jq -r '.slack_webhook_url // ""' "${OPTIONS_JSON}")
PG_HOST=$(jq -r '.pg_host // ""' "${OPTIONS_JSON}")
PG_PORT=$(jq -r '.pg_port // 5432' "${OPTIONS_JSON}")
PG_DATABASE=$(jq -r '.pg_database // "energy"' "${OPTIONS_JSON}")
PG_USER=$(jq -r '.pg_user // ""' "${OPTIONS_JSON}")
PG_PASSWORD=$(jq -r '.pg_password // ""' "${OPTIONS_JSON}")
LOG_LEVEL=$(jq -r '.log_level // "INFO"' "${OPTIONS_JSON}")

# HA token: prefer explicit option; fall back to Supervisor-provided token.
# When running as an addon, SUPERVISOR_TOKEN grants access to HA REST API
# at http://supervisor/core — no separate token needed.
if [[ -z "${HA_TOKEN}" ]]; then
    if [[ -n "${SUPERVISOR_TOKEN:-}" ]]; then
        HA_TOKEN="${SUPERVISOR_TOKEN}"
        log_info "Using SUPERVISOR_TOKEN as HA token (addon mode)."
    else
        log_error "No ha_token in options and SUPERVISOR_TOKEN not set."
        log_error "Set ha_token in addon options or ensure this runs under HA Supervisor."
        exit 1
    fi
fi

# Export all secrets as the environment variables carma-box expects.
export CARMA_HA_TOKEN="${HA_TOKEN}"
export CARMA_SOLCAST_API_KEY="${SOLCAST_KEY}"
export CARMA_NORDPOOL_API_KEY="${NORDPOOL_KEY}"
export CARMA_SLACK_WEBHOOK="${SLACK_WEBHOOK}"
export CARMA_PG_USER="${PG_USER}"
export CARMA_PG_PASS="${PG_PASSWORD}"

# Never log secret values — only confirm they are present or absent.
log_info "CARMA_HA_TOKEN     : $([ -n "${HA_TOKEN}" ] && echo 'set' || echo 'MISSING')"
log_info "CARMA_SOLCAST_KEY  : $([ -n "${SOLCAST_KEY}" ] && echo 'set' || echo 'empty (Solcast disabled)')"
log_info "CARMA_NORDPOOL_KEY : $([ -n "${NORDPOOL_KEY}" ] && echo 'set' || echo 'empty (HA entity used)')"
log_info "CARMA_SLACK_WEBHOOK: $([ -n "${SLACK_WEBHOOK}" ] && echo 'set' || echo 'empty (Slack disabled)')"
log_info "PG hub sync        : $([ -n "${PG_HOST}" ] && echo "enabled → ${PG_HOST}:${PG_PORT}/${PG_DATABASE}" || echo 'disabled (pg_host empty)')"
log_info "LOG_LEVEL          : ${LOG_LEVEL}"

# ---------------------------------------------------------------------------
# 3. Validate site.yaml
# ---------------------------------------------------------------------------

if [[ ! -f "${SITE_YAML}" ]]; then
    log_error "site.yaml not found at ${SITE_YAML}"
    log_error ""
    log_error "CARMA Box requires a full site configuration."
    log_error "Create the file at /homeassistant/carmabox/site.yaml"
    log_error "(accessible via HA File Editor or SSH addon)."
    log_error ""
    log_error "Minimum required fields:"
    log_error "  site: { id: ..., name: ..., latitude: ..., longitude: ... }"
    log_error "  homeassistant: { url: 'http://supervisor/core' }"
    log_error "  batteries: [...]"
    log_error "  ev_charger: { ... }"
    log_error "  ev: { ... }"
    log_error ""
    log_error "See /homeassistant/carmabox/site.yaml.example for a full template."
    exit 1
fi

log_info "site.yaml found at ${SITE_YAML}"

# ---------------------------------------------------------------------------
# 4. Ensure /data/ layout and SQLite integrity
# ---------------------------------------------------------------------------

mkdir -p "${LOG_DIR}"

# On first start (no DB) or after addon update: DB will be created by carma-box.
# On subsequent starts: run a quick integrity check before handing off.
if [[ -f "${DB_PATH}" ]]; then
    log_info "Checking SQLite integrity: ${DB_PATH}"
    INTEGRITY=$(sqlite3 "${DB_PATH}" "PRAGMA integrity_check;" 2>&1 || true)
    if [[ "${INTEGRITY}" != "ok" ]]; then
        log_error "SQLite integrity check FAILED: ${INTEGRITY}"
        log_error "The state database may be corrupted."
        log_error "To recover: stop addon, delete /data/carma.db, restart addon."
        log_error "Cycle history will be lost but operation resumes immediately."
        exit 1
    fi
    log_info "SQLite integrity: ok"
else
    log_info "No existing state.db — will be created on first run."
fi

# ---------------------------------------------------------------------------
# 5. Copy site.yaml example if homeassistant/carmabox/ is freshly created
# ---------------------------------------------------------------------------

if [[ -f "/app/config/site.yaml.example" && ! -f "/homeassistant/carmabox/site.yaml.example" ]]; then
    mkdir -p "/homeassistant/carmabox"
    cp "/app/config/site.yaml.example" "/homeassistant/carmabox/site.yaml.example"
    log_info "Copied site.yaml.example to /homeassistant/carmabox/site.yaml.example"
fi

# ---------------------------------------------------------------------------
# 6. Override log + DB paths via environment variables.
#    main.py reads these from the site.yaml, but the addon-managed paths
#    take precedence via CARMA_OVERRIDE_* env vars checked in main.py.
# ---------------------------------------------------------------------------

export CARMA_OVERRIDE_LOG_FILE="${LOG_FILE}"
export CARMA_OVERRIDE_DB_PATH="${DB_PATH}"
export CARMA_OVERRIDE_LOG_LEVEL="${LOG_LEVEL}"

# HA Supervisor HA URL when running as an addon.
# If site.yaml already has homeassistant.url set to something other than
# localhost, that value wins. This env var is a fallback default.
export CARMA_HA_URL_DEFAULT="http://supervisor/core"

# ---------------------------------------------------------------------------
# 7. Exec main.py — replaces this shell process.
#    SIGTERM from Supervisor propagates directly to Python for clean shutdown.
# ---------------------------------------------------------------------------

log_info "Starting CARMA Box (main.py) with site config: ${SITE_YAML}"
log_info "State DB: ${DB_PATH}"
log_info "Log file: ${LOG_FILE}"

exec python3 "${APP_DIR}/main.py" --config "${SITE_YAML}"
