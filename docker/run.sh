#!/bin/sh
#
# HSYNC Server - Docker Run Script
# Starts the uvicorn server with the configured settings.
#
set -e

# Default values
HSYNC_LOG_LEVEL="${HSYNC_LOG_LEVEL:-info}"
HSYNC_PORT="${HSYNC_PORT:-8765}"
HSYNC_WORKERS="${HSYNC_WORKERS:-1}"

exec uvicorn hsync_server.main:app \
    --host 0.0.0.0 \
    --port "${HSYNC_PORT}" \
    --log-config "config/logging-${HSYNC_LOG_LEVEL}.yaml" \
    --log-level "${HSYNC_LOG_LEVEL}" \
    --workers "${HSYNC_WORKERS}"