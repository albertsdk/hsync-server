#!/bin/sh
set -e

if [ "$1" = 'uvicorn' ]; then
    exec su-exec nobody $*
else
    exec "$@"
fi

