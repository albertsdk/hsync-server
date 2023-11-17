#!/bin/sh

uvicorn hsync_server.main:app \
    --host 0.0.0.0 --port 8765 \
    --log-config config/logging-${HSYNC_LOG_LEVEL}.yaml \
    --log-level ${HSYNC_LOG_LEVEL} \
    --workers 1