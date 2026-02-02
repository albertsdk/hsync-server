"""
Rate limiting configuration for HSYNC Server.

This module provides rate limiting functionality using slowapi to protect
the server from abuse and ensure fair usage across all clients.

Default limits:
- General API: 100 requests/minute per IP
- Upload: 50 requests/minute per IP
- Download: 200 requests/minute per IP
- WebSocket: 10 connections/minute per IP
"""

from fastapi import Request, Response
from logging import getLogger
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from starlette.responses import JSONResponse


# Logger instance for this module
LOG = getLogger("main")


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
    default_limits=["100/minute"],
    headers_enabled=True,  # Add rate limit headers to responses
)


async def rate_limit_exceeded_handler(
    request: Request, exc: RateLimitExceeded
) -> Response:
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
    LOG.warning(
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


# Rate limit decorators for different endpoint types
# Use these as decorators on route handlers

# Standard rate limit for general endpoints
RATE_LIMIT_DEFAULT = "100/minute"

# Rate limit for upload endpoints (more restrictive)
RATE_LIMIT_UPLOAD = "50/minute"

# Rate limit for download endpoints (more permissive)
RATE_LIMIT_DOWNLOAD = "200/minute"

# Rate limit for WebSocket connections
RATE_LIMIT_WEBSOCKET = "10/minute"


LOG.info(f"Module {__name__} loaded")

