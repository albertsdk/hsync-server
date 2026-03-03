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

from .api import admin as admin_api  # noqa: F401
from .api import (  # noqa: F401
    buckets,
    events,
    files,
    health,
    members,  # noqa: F401
    presence,
)
from .auth_store import get_auth_store
from .config import get_buckets_path, get_cors_enabled, get_cors_origins, get_settings
from .constants import HEADER_CLIENT_NAME, HEADER_CLIENT_VERSION, HSYNC_VERSION
from .error_handlers import register_exception_handlers
from .hashes import set_sha256_hashes
from .middleware import RequestIdMiddleware, VersionMiddleware
from .rate_limiter import limiter, rate_limit_exceeded_handler
from .routers import auth_router, public_router


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """
    Bootstrap admin account, warm up server state.

    Resolved during application startup (after env files have been loaded
    by the entry point) instead of at module-import time so that values
    reflect the effective runtime configuration.
    """
    start = time.time()

    # Bootstrap the admin account (creates one if the table is empty).
    from .config import get_admin_key

    env_secret = get_admin_key()
    store = get_auth_store()
    generated_token = store.bootstrap_admin(env_secret)
    # generated_token is non-None only when a secret was generated and printed
    # (the warning with the token is already logged inside bootstrap_admin).

    # Log account status (no secrets).
    accounts = store.list_accounts()
    active = sum(1 for a in accounts if a.status == "active")
    log.info(f"Accounts: {len(accounts)} total, {active} active")

    # Preload SHA-256 checksums for all existing files in buckets.
    buckets_path = get_buckets_path()
    log.info(f"Loading SHA-256 file hashes from: {buckets_path}")
    sha256_files = set_sha256_hashes(buckets_path)
    log.info(f"{sha256_files} files stored.")

    # Log available logging levels for debugging configuration.
    log.debug("Available logging: DEBUG")
    log.info("Available logging: INFO")
    log.warning("Available logging: WARNING")

    # Log total startup time.
    tot_time = round(time.time() - start, 3)
    log.info(f"HSYNC Server started in: {tot_time} sec.")

    yield


# Initialize FastAPI application with metadata
# API docs (Swagger UI / ReDoc) are disabled by default in production;
# set HSYNC_ENABLE_DOCS=true for development.
_settings = get_settings()
app = FastAPI(
    title="HSYNC - HTTP Sync file server",
    version=HSYNC_VERSION,
    lifespan=lifespan,
    docs_url="/docs" if _settings.enable_docs else None,
    redoc_url="/redoc" if _settings.enable_docs else None,
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
# Auth router: endpoints that require account token authentication
app.include_router(auth_router)
# Admin router: endpoints that require admin role
app.include_router(admin_api.admin_router)

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
        allow_headers=["Authorization", "Content-Type", HEADER_CLIENT_NAME, HEADER_CLIENT_VERSION],
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
