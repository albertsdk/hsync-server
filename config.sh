#!/bin/bash

# Enviromìnment
export VENV_PATH=.venv

#
# HSYNC FILESERVER
#
if [ -z "${HSYNC_LOG_LEVEL}" ]; then
    export HSYNC_LOG_LEVEL="info"
fi

export HSYNC_PATH="/app"
export HSYNC_IP="0.0.0.0"
export HSYNC_PORT=8765
