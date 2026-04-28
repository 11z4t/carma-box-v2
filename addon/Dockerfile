# CARMA Box — Home Assistant Supervisor Addon
# PLAT-1813: HA-addon packaging
#
# Build context: addon/ directory (HA Supervisor rule).
# Application code is cloned from GitHub at build time so this Dockerfile
# works correctly regardless of build context.
#
# HA Supervisor provides BUILD_FROM automatically for each target arch.
# See: https://developers.home-assistant.io/docs/add-ons/configuration#base-images

ARG BUILD_FROM=ghcr.io/hassio-addons/base-python:3.12

# hadolint ignore=DL3006
FROM ${BUILD_FROM}

# ---------------------------------------------------------------------------
# System-level packages
# ---------------------------------------------------------------------------

# Install only what carma-box strictly needs at runtime.
# git: used to clone application code at image build time.
# sqlite3 CLI: used by run.sh for integrity check on first start.
# bash + jq: run.sh dependencies (jq parses /data/options.json).
RUN apk add --no-cache \
    bash \
    git \
    jq \
    sqlite \
    tzdata \
    && rm -rf /var/cache/apk/*

# ---------------------------------------------------------------------------
# Python application
# Clone from GitHub at build time. CARMA_VERSION pins to a specific
# commit, branch, or tag so every image build is reproducible.
# ---------------------------------------------------------------------------

ARG CARMA_REPO=https://github.com/11z4t/carma-box-v2.git
ARG CARMA_VERSION=master

WORKDIR /app
RUN git clone --depth 1 --branch "${CARMA_VERSION}" "${CARMA_REPO}" . \
    && pip3 install --no-cache-dir --requirement requirements.txt \
    && rm -rf .git tests htmlcov docs

# ---------------------------------------------------------------------------
# Addon entrypoint
# run.sh is in the addon/ directory (= HA build context), so COPY works.
# ---------------------------------------------------------------------------

COPY run.sh /run.sh
RUN chmod a+x /run.sh

# Persistent data directory — HA Supervisor maps /data/ to addon storage.
# State DB and logs live here and survive addon restarts and updates.
RUN mkdir -p /data/logs

# Health check — HA Supervisor can poll this to detect a stuck addon.
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD wget -qO- "http://127.0.0.1:8412/health" | grep -q '"status":"ok"' || exit 1

CMD ["/run.sh"]
