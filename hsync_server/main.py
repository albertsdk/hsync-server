# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
HSYNC Server - Main Application Entry Point.

This module initializes the FastAPI application, configures routers,
middleware, rate limiting, and Prometheus metrics instrumentation.
It also preloads MD5 hashes for all existing files in the buckets
directory on startup.
"""

import sys
import time

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from loguru import logger as LOG
from prometheus_fastapi_instrumentator import Instrumentator
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from .api import buckets, events, files, health  # noqa: F401
from .config import HSYNC_API_KEY, HSYNC_BUCKETS, HSYNC_CORS_ENABLED, HSYNC_CORS_ORIGINS
from .constants import HSYNC_VERSION
from .error_handlers import register_exception_handlers
from .hashes import set_md5_hashes
from .middleware import RequestIdMiddleware, VersionMiddleware
from .rate_limiter import limiter, rate_limit_exceeded_handler
from .routers import auth_router, public_router

# =============================================================================
# Security Validation at Startup
# =============================================================================

# Validate that API key is configured
if not HSYNC_API_KEY:
    LOG.critical(
        "HSYNC_API_KEY environment variable is not set. "
        "Server cannot start without authentication configured. "
        "Generate a secure key at: https://it-tools.tech/token-generator"
    )
    sys.exit(1)

if len(HSYNC_API_KEY) < 16:
    LOG.warning(
        f"HSYNC_API_KEY is only {len(HSYNC_API_KEY)} characters. "
        "Consider using a longer key (32+ characters) for better security."
    )

# Track startup time for performance monitoring
start = time.time()

# Initialize FastAPI application with metadata
app = FastAPI(
    title="HSYNC - HTTP Sync file server",
    version=HSYNC_VERSION,
)

# Configure rate limiting
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)  # type: ignore[arg-type]
app.add_middleware(SlowAPIMiddleware)

# Register custom exception handlers for standardized error responses
register_exception_handlers(app)

# Register API routers
# Public router: endpoints that don't require authentication
app.include_router(public_router)
# Auth router: endpoints that require API key authentication
# (buckets.py and files.py use auth_router directly via decorators)
app.include_router(auth_router)

# Add GZip compression middleware for responses larger than 1000 bytes
app.add_middleware(GZipMiddleware, minimum_size=1000)

# Add CORS middleware if enabled (for web frontend compatibility)
if HSYNC_CORS_ENABLED:
    LOG.info(f"CORS enabled for origins: {HSYNC_CORS_ORIGINS or ['*']}")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=HSYNC_CORS_ORIGINS or ["*"],
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Client-Name", "X-Client-Version"],
    )

# Add Request ID middleware for distributed tracing
# This must be added after other middleware to ensure request_id is set first
app.add_middleware(RequestIdMiddleware)

# Add Version middleware for client-server version compatibility checking
# This adds X-Server-Version header to all responses and rejects incompatible clients
app.add_middleware(VersionMiddleware)

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
