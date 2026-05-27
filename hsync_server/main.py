# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
HSYNC Server - Main Application Entry Point.

This module initializes the FastAPI application, configures routers,
middleware, rate limiting, and Prometheus metrics instrumentation.
It also preloads SHA-256 hashes for all existing files in the buckets
directory on startup.
"""

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from loguru import logger as log
from prometheus_fastapi_instrumentator import Instrumentator
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from .api import buckets, events, files, health  # noqa: F401
from .config import get_api_key, get_buckets_path, get_cors_enabled, get_cors_origins
from .constants import HSYNC_VERSION
from .error_handlers import register_exception_handlers
from .hashes import set_sha256_hashes
from .middleware import RequestIdMiddleware, VersionMiddleware
from .rate_limiter import limiter, rate_limit_exceeded_handler
from .routers import auth_router, public_router


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """
    Validate runtime configuration and warm up server state.

    Resolved during application startup (after env files have been loaded
    by the entry point) instead of at module-import time so that values
    reflect the effective runtime configuration.
    """
    start = time.time()

    # Validate that API key is configured
    api_key = get_api_key()
    if not api_key:
        log.critical(
            "HSYNC_API_KEY environment variable is not set. "
            "Server cannot start without authentication configured. "
            "Generate a secure key at: https://it-tools.tech/token-generator"
        )
        raise RuntimeError("HSYNC_API_KEY is not set")

    if len(api_key) < 16:
        log.warning(
            f"HSYNC_API_KEY is only {len(api_key)} characters. "
            "Consider using a longer key (32+ characters) for better security."
        )

    # Preload SHA-256 checksums for all existing files in buckets
    # This enables fast duplicate detection without recalculating hashes
    buckets_path = get_buckets_path()
    log.info(f"Loading SHA-256 file hashes from: {buckets_path}")
    sha256_files = set_sha256_hashes(buckets_path)
    log.info(f"{sha256_files} files stored.")

    # Log available logging levels for debugging configuration
    log.debug("Available logging: DEBUG")
    log.info("Available logging: INFO")
    log.warning("Available logging: WARNING")

    # Log total startup time
    tot_time = round(time.time() - start, 3)
    log.info(f"HSYNC Server started in: {tot_time} sec.")

    yield


# Initialize FastAPI application with metadata
app = FastAPI(
    title="HSYNC - HTTP Sync file server",
    version=HSYNC_VERSION,
    lifespan=lifespan,
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
if get_cors_enabled():
    cors_origins = get_cors_origins()
    log.info(f"CORS enabled for origins: {cors_origins or ['*']}")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins or ["*"],
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

# Configure Prometheus metrics instrumentation
# Excludes documentation and monitoring endpoints from metrics collection
Instrumentator(
    excluded_handlers=["/docs", "/metrics", "/health"],
).instrument(app).expose(app, tags=["Public Router"], endpoint="/metrics")
