"""
HSYNC Server - Main Application Entry Point.

This module initializes the FastAPI application, configures routers,
middleware, and Prometheus metrics instrumentation. It also preloads
MD5 hashes for all existing files in the buckets directory on startup.
"""

import time

from .api import events, files, health
from .config import HSYNC_BUCKETS
from .file_hashes import set_md5_hashes
from .routers import auth_router, public_router
from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from logging import getLogger
from prometheus_fastapi_instrumentator import Instrumentator


# Logger instance for this module
LOG = getLogger("main")

# Track startup time for performance monitoring
start = time.time()

# Initialize FastAPI application with metadata
app = FastAPI(
    title="HSYNC - HTTP Sync file server",
    version="0.1.0",
)

# Register API routers
# Public router: endpoints that don't require authentication
app.include_router(public_router)
# Auth router: endpoints that require API key authentication
app.include_router(auth_router)
# Events router: WebSocket endpoint for real-time synchronization
app.include_router(events.public_router)
# Files router: file and bucket management endpoints
app.include_router(files.auth_router)
# Health router: health check endpoint for monitoring
app.include_router(health.public_router)

# Add GZip compression middleware for responses larger than 1000 bytes
app.add_middleware(GZipMiddleware, minimum_size=1000)

# Preload MD5 checksums for all existing files in buckets
# This enables fast duplicate detection without recalculating hashes
LOG.info(f"Loading MD5 file hashes from: {HSYNC_BUCKETS}")
md5_files = set_md5_hashes(HSYNC_BUCKETS)
LOG.info(f"{md5_files} files stored.")

# Configure Prometheus metrics instrumentation
# Excludes documentation and monitoring endpoints from metrics collection
Instrumentator(
    excluded_handlers=["/docs", "/metrics", "/health"],
).instrument(
    app
).expose(app, tags=["Public Router"], endpoint="/metrics")

# Log available logging levels for debugging configuration
LOG.debug("Available logging: DEBUG")
LOG.info("Available logging: INFO")
LOG.warning("Available logging: WARNING")

# Log total startup time
tot_time = round(time.time() - start, 3)
LOG.info(f"HSYNC Server started in: {tot_time} sec.")
