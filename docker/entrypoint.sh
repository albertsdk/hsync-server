#!/bin/sh
#
# HSYNC Server - Docker Entrypoint
# Handles process execution with proper user permissions.
#
set -e

# Run uvicorn as nobody user for security
if [ "$1" = 'uvicorn' ]; then
    exec su-exec nobody "$@"
else
    exec "$@"
fi
