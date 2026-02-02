#!/usr/bin/env bash
#
# HSYNC Server - Configuration
# Environment variables for the HSYNC server.
#
# Usage: source config.sh
#

# -----------------------------------------------------------------------------
# Virtual Environment
# -----------------------------------------------------------------------------
export VENV_PATH=".venv"

# -----------------------------------------------------------------------------
# Server Configuration
# -----------------------------------------------------------------------------

# Log level: debug, info, warning, error
export HSYNC_LOG_LEVEL="${HSYNC_LOG_LEVEL:-info}"

# Application path
export HSYNC_PATH="${HSYNC_PATH:-/app}"

# Server bind address
export HSYNC_IP="${HSYNC_IP:-0.0.0.0}"

# Server port
export HSYNC_PORT="${HSYNC_PORT:-8765}"
