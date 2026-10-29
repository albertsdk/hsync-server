# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""
HSYNC Server - Main Application Entry Point.

This module initializes the FastAPI application, configures routers,
middleware, rate limiting, and Prometheus metrics instrumentation.
It also preloads SHA-256 hashes for all existing files in the buckets
directory on startup.
"""

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from hsync_common.protocol.rest import REST_HEALTH_PATH, REST_METRICS_PATH
from loguru import logger as log
from prometheus_fastapi_instrumentator import Instrumentator
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from .api import admin as admin_api
from .api import (  # noqa: F401
    buckets,
    events,
    files,
    health,
    members,
    presence,
)
from .auth_store import get_auth_store
from .config import (
    get_buckets_path,
    get_cors_enabled,
    get_cors_origins,
    get_max_request_body_size,
    get_settings,
)
from .constants import (
    HEADER_CLIENT_NAME,
    HEADER_CLIENT_VERSION,
    HEADER_PROTOCOL_VERSION,
    HSYNC_VERSION,
)
from .error_handlers import register_exception_handlers
from .hashes import set_sha256_hashes
from .middleware import (
    MaxRequestSizeMiddleware,
    RequestIdMiddleware,
    TransferAwareGZipMiddleware,
    VersionMiddleware,
)
from .rate_limiter import limiter, rate_limit_exceeded_handler
from .routers import auth_router, public_router
from .state_maintenance import (
    run_periodic_incremental_vacuum,
    vacuum_on_shutdown,
    vacuum_on_startup,
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """
    Rendezvous admin account, warm up server state.

    Resolved during application startup (after env files have been loaded
    by the entry point) instead of at module-import time so that values
    reflect the effective runtime configuration.
    """
    start = time.time()

    # Rendezvous the admin account (creates one if the table is empty).
    from .config import get_admin_key

    env_secret = get_admin_key()
    store = get_auth_store()
    # The rendezvous returns its generated token only when a secret was minted;
    # the warning with the token is already logged inside rendezvous_admin.
    store.rendezvous_admin(env_secret)

    # Log account status (no secrets).
    accounts = store.list_accounts()
    active = sum(1 for a in accounts if a.status == "active")
    log.info(f"Accounts: {len(accounts)} total, {active} active")

    # Preload SHA-256 checksums for all existing files in buckets.
    buckets_path = get_buckets_path()
    log.info(f"Loading SHA-256 file hashes from: {buckets_path}")
    sha256_files = set_sha256_hashes(buckets_path)
    log.info(f"{sha256_files} files stored.")

    # Reclaim free space left by a previous session (best-effort; runs before
    # serving, so the synchronous VACUUM cannot race a request).
    vacuum_on_startup()

    # Start the periodic in-place compaction of the state DB; it is cancelled
    # during shutdown below.
    maintenance_stop = asyncio.Event()
    maintenance_task = asyncio.create_task(run_periodic_incremental_vacuum(maintenance_stop))

    tot_time = round(time.time() - start, 3)
    log.info(f"HSYNC Server started in: {tot_time} sec.")

    try:
        yield
    finally:
        maintenance_stop.set()
        await maintenance_task
        vacuum_on_shutdown()


def configure_cors(app: FastAPI, *, enabled: bool, origins: list[str]) -> None:
    """Attach CORS middleware only for an explicit origin allow-list.

    A wildcard is never combined with credentials: the API authenticates with a
    Bearer token, so ``allow_credentials`` is not needed, and silently
    reflecting any Origin would let a hostile page read authenticated
    responses. Enabling CORS without origins is therefore a no-op (with a
    warning), not an implicit wildcard.
    """
    if not enabled:
        return
    if not origins:
        log.warning("CORS enabled without HSYNC_CORS_ORIGINS: CORS disabled")
        return
    log.info(f"CORS enabled for origins: {origins}")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,  # never "*"
        allow_credentials=False,  # auth is Bearer, not cookie-based
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            HEADER_CLIENT_NAME,
            HEADER_CLIENT_VERSION,
            HEADER_PROTOCOL_VERSION,
        ],
    )


# API docs (Swagger UI / ReDoc) are disabled by default in production;
# set HSYNC_ENABLE_DOCS=true for development.
_settings = get_settings()
app = FastAPI(
    title="HSYNC - HTTP Sync file server",
    version=HSYNC_VERSION,
    lifespan=lifespan,
    docs_url="/docs" if _settings.enable_docs else None,
    redoc_url="/redoc" if _settings.enable_docs else None,
    openapi_url="/openapi.json" if _settings.enable_docs else None,
)

# Configure rate limiting
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)  # type: ignore[arg-type]
app.add_middleware(SlowAPIMiddleware)

# Register custom exception handlers for standardized error responses
register_exception_handlers(app)

# Public router: endpoints that don't require authentication
app.include_router(public_router)
# Auth router: endpoints that require account token authentication
app.include_router(auth_router)
# Admin router: endpoints that require admin role
app.include_router(admin_api.admin_router)

# Add GZip compression middleware for responses larger than 1000 bytes.
# Transfer payloads (file bytes and delta literals) are excluded: their coding
# is the sync protocol's own (identity | zstd) and clients reject anything else.
app.add_middleware(TransferAwareGZipMiddleware, minimum_size=1000)

# Reject oversized request bodies at the ASGI receive boundary. FastAPI parses
# and spools multipart bodies before the auth dependency runs, so this is the
# only layer that bounds what an unauthenticated client can make the server
# buffer on disk/in memory.
_max_body_size = get_max_request_body_size()
if _max_body_size <= 0:
    log.warning(
        "Request-body cap disabled (HSYNC_MAX_REQUEST_BODY_SIZE=0 with "
        "HSYNC_MAX_UPLOAD_SIZE=0); request bodies are unbounded"
    )
app.add_middleware(MaxRequestSizeMiddleware, max_size=_max_body_size)

# Add CORS middleware only for an explicit origin allow-list (see helper).
configure_cors(app, enabled=get_cors_enabled(), origins=get_cors_origins())

# Add Request ID middleware for distributed tracing
# This must be added after other middleware to ensure request_id is set first
app.add_middleware(RequestIdMiddleware)

# Add Version middleware for client-server version compatibility checking
# This adds X-Server-Version header to all responses and rejects incompatible clients
app.add_middleware(VersionMiddleware)

# Configure Prometheus metrics instrumentation.
# Excludes documentation and monitoring endpoints from metrics collection.
# The /metrics endpoint itself is served by an authenticated route in
# ``hsync_server.metrics`` (it must not be public).
Instrumentator(
    excluded_handlers=["/docs", REST_METRICS_PATH, REST_HEALTH_PATH],
).instrument(app)
