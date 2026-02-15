# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
Rate limiting configuration for HSYNC Server.

This module provides rate limiting functionality using slowapi to protect
the server from abuse and ensure fair usage across all clients.

Default limits (configurable via environment variables):
- HSYNC_RATE_LIMIT_DEFAULT: 500/minute per IP (general API)
- HSYNC_RATE_LIMIT_UPLOAD: 200/minute per IP (file uploads)
- HSYNC_RATE_LIMIT_DOWNLOAD: 1000/minute per IP (file downloads)
- HSYNC_RATE_LIMIT_WEBSOCKET: 50/minute per IP (WebSocket connections)

Format: "<number>/<period>" where period is: second, minute, hour, day
Examples: "100/minute", "1000/hour", "10/second"
"""

import os

from fastapi import Request, Response
from loguru import logger as log
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from starlette.responses import JSONResponse


# -----------------------------------------------------------------------------
# Rate Limit Configuration (from environment variables)
# -----------------------------------------------------------------------------

# Standard rate limit for general endpoints (list, delete, bucket operations)
RATE_LIMIT_DEFAULT: str = os.environ.get("HSYNC_RATE_LIMIT_DEFAULT", "500/minute")

# Rate limit for upload endpoints (more restrictive due to resource usage)
RATE_LIMIT_UPLOAD: str = os.environ.get("HSYNC_RATE_LIMIT_UPLOAD", "200/minute")

# Rate limit for download endpoints (more permissive for high-traffic systems)
RATE_LIMIT_DOWNLOAD: str = os.environ.get("HSYNC_RATE_LIMIT_DOWNLOAD", "1000/minute")

# Rate limit for WebSocket connections
RATE_LIMIT_WEBSOCKET: str = os.environ.get("HSYNC_RATE_LIMIT_WEBSOCKET", "50/minute")


def get_client_ip(request: Request) -> str:
    """
    Extract client IP address from request.

    Checks X-Forwarded-For header first (for proxied requests),
    then falls back to the direct client address.

    Args:
        request: The incoming FastAPI request.

    Returns:
        Client IP address as string.
    """
    # Check for X-Forwarded-For header (common in proxy setups)
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        # Take the first IP in the chain (original client)
        return forwarded_for.split(",")[0].strip()
    
    # Fall back to direct client address
    return get_remote_address(request)


# Initialize the rate limiter with custom key function
limiter = Limiter(
    key_func=get_client_ip,
    default_limits=[RATE_LIMIT_DEFAULT],
    headers_enabled=True,  # Add rate limit headers to responses
)


async def rate_limit_exceeded_handler(
    request: Request, exc: Exception
) -> JSONResponse:
    """
    Custom handler for rate limit exceeded errors.

    Returns a JSON response with details about the rate limit
    and when the client can retry.

    Args:
        request: The incoming request that exceeded the limit.
        exc: The rate limit exception with limit details.

    Returns:
        JSON response with 429 status code.
    """
    # Type check to ensure we have RateLimitExceeded
    if not isinstance(exc, RateLimitExceeded):
        return JSONResponse(
            status_code=500,
            content={"error": "Internal server error"},
        )

    log.warning(
        f"Rate limit exceeded for {get_client_ip(request)}: "
        f"{exc.detail}"
    )
    return JSONResponse(
        status_code=429,
        content={
            "error": "Rate limit exceeded",
            "detail": str(exc.detail),
            "retry_after": getattr(exc, "retry_after", 60),
        },
        headers={
            "Retry-After": str(getattr(exc, "retry_after", 60)),
        },
    )


log.info(
    f"Rate limits configured: default={RATE_LIMIT_DEFAULT}, "
    f"upload={RATE_LIMIT_UPLOAD}, download={RATE_LIMIT_DOWNLOAD}, "
    f"websocket={RATE_LIMIT_WEBSOCKET}"
)

