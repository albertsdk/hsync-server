# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""
Rate limiting configuration for HSYNC Server.

This module provides rate limiting functionality using slowapi to protect
the server from abuse and ensure fair usage across all clients.

Default limits (configurable via environment variables):
- HSYNC_RATE_LIMIT_DEFAULT: 500/minute per IP (general API)
- HSYNC_RATE_LIMIT_UPLOAD: 200/minute per IP (file uploads)
- HSYNC_RATE_LIMIT_DOWNLOAD: 1000/minute per IP (file downloads)
- HSYNC_RATE_LIMIT_WEBSOCKET: 50/minute per IP (WebSocket connections)
- HSYNC_RATE_LIMIT_WS_MESSAGES: 10/second per connection (WebSocket events)

Reverse-proxy handling:
- HSYNC_TRUSTED_PROXIES: comma-separated list of trusted reverse-proxy IPs.
  X-Forwarded-For is only honoured when the direct peer is one of these IPs;
  otherwise the header is ignored to prevent IP spoofing / rate-limit bypass.
  Empty (default) = no proxy trusted, always use the direct peer address.

Format: "<number>/<period>" where period is: second, minute, hour, day
Examples: "100/minute", "1000/hour", "10/second"
"""

import ipaddress
import re
import time

from fastapi import Request
from loguru import logger as log
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.responses import JSONResponse

from hsync_server.config import (
    get_rate_limit_admin,
    get_rate_limit_default,
    get_rate_limit_delta,
    get_rate_limit_download,
    get_rate_limit_upload,
    get_rate_limit_ws_messages,
    get_trusted_proxies,
)

# -----------------------------------------------------------------------------
# Rate Limit Configuration (from Settings / environment variables)
# -----------------------------------------------------------------------------

# Standard rate limit for general endpoints (list, delete, bucket operations)
RATE_LIMIT_DEFAULT: str = get_rate_limit_default()

# Rate limit for upload endpoints (more restrictive due to resource usage)
RATE_LIMIT_UPLOAD: str = get_rate_limit_upload()

# Rate limit for download endpoints (more permissive for high-traffic systems)
RATE_LIMIT_DOWNLOAD: str = get_rate_limit_download()

# Rate limit for admin endpoints (account/key management — stricter than default)
RATE_LIMIT_ADMIN: str = get_rate_limit_admin()

# Rate limit for delta transfer endpoints (signature/matching is CPU-bound)
RATE_LIMIT_DELTA: str = get_rate_limit_delta()

# Trusted reverse-proxy IPs allowed to set X-Forwarded-For. Empty by default so
# the header is ignored unless the deployment explicitly opts in.
TRUSTED_PROXIES: frozenset[str] = get_trusted_proxies()


def _rightmost_untrusted_hop(forwarded_for: str, trusted: frozenset[str]) -> str | None:
    """Return the closest client IP that was not added by a trusted proxy.

    Reverse proxies append the peer they saw, so the rightmost entries are the
    most trustworthy. Walking right-to-left and skipping trusted proxies avoids
    trusting a client-supplied prefix (``X-Forwarded-For: 1.2.3.4`` forwarded
    verbatim by ``$proxy_add_x_forwarded_for``). A malformed chain is ignored
    entirely rather than used as a rate-limit key.
    """
    hops = [hop.strip() for hop in forwarded_for.split(",") if hop.strip()]
    for hop in reversed(hops):
        if hop in trusted:
            continue
        try:
            ipaddress.ip_address(hop)
        except ValueError:
            return None
        return hop
    return None


def resolve_client_ip(peer: str | None, forwarded_for: str | None) -> str:
    """Resolve a client IP when there is no Starlette ``Request`` (WebSocket).

    Applies the same trusted-proxy rule as :func:`get_client_ip`: the header is
    only consulted when the direct peer is a configured proxy, and the
    rightmost untrusted hop is used.
    """
    if peer and peer in TRUSTED_PROXIES and forwarded_for:
        hop = _rightmost_untrusted_hop(forwarded_for, TRUSTED_PROXIES)
        if hop is not None:
            return hop
    return peer or "unknown"


def get_client_ip(request: Request) -> str:
    """
    Extract client IP address from request.

    X-Forwarded-For is only honoured when the request arrives directly from a
    trusted reverse proxy (HSYNC_TRUSTED_PROXIES); otherwise an attacker could
    spoof the header to bypass rate limiting. Falls back to the direct peer
    address in all other cases.

    Args:
        request: The incoming FastAPI request.

    Returns:
        Client IP address as string.
    """
    # Only trust X-Forwarded-For when the direct peer is a configured proxy
    client_host = request.client.host if request.client else None
    if client_host and client_host in TRUSTED_PROXIES:
        forwarded_for = request.headers.get("X-Forwarded-For")
        if forwarded_for:
            hop = _rightmost_untrusted_hop(forwarded_for, TRUSTED_PROXIES)
            if hop is not None:
                return hop

    # Fall back to direct client address
    return client_host or get_remote_address(request)


limiter = Limiter(
    key_func=get_client_ip,
    default_limits=[RATE_LIMIT_DEFAULT],
    headers_enabled=True,  # Add rate limit headers to responses
)


async def rate_limit_exceeded_handler(request: Request, exc: Exception) -> JSONResponse:
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

    log.warning(f"Rate limit exceeded for {get_client_ip(request)}: {exc.detail}")
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
    f"upload={RATE_LIMIT_UPLOAD}, download={RATE_LIMIT_DOWNLOAD}"
)


# -----------------------------------------------------------------------------
# WebSocket Message Rate Limiting (Token Bucket)
# -----------------------------------------------------------------------------

# Rate limit for WebSocket event messages per connection
# Accepted periods: second, minute, hour, day.
WS_MESSAGES_DEFAULT: str = get_rate_limit_ws_messages()

_WS_RATE_PATTERN = re.compile(r"^(\d+)/(second|minute|hour|day)$")

# Precomputed period multipliers (tokens/second) for _parse_ws_rate: built once
# at import instead of re-allocating the dict on every WS connection.
_WS_RATE_MULTIPLIERS: dict[str, float] = {
    "second": 1.0,
    "minute": 1.0 / 60,
    "hour": 1.0 / 3600,
    "day": 1.0 / 86400,
}


def _parse_ws_rate(rate_str: str) -> tuple[float, int]:
    """
    Parse a rate string like "10/second" into (tokens_per_second, burst).

    Burst is set to 2x the rate per second to allow short spikes.

    Args:
        rate_str: Rate string in format "<number>/<period>".

    Returns:
        Tuple of (tokens_per_second, burst_capacity).
    """
    match = _WS_RATE_PATTERN.match(rate_str)
    if not match:
        log.warning(f"Invalid WS rate limit format: {rate_str}, using default")
        return 10.0, 20

    amount = float(match.group(1))
    period = match.group(2)

    rate = amount * _WS_RATE_MULTIPLIERS[period]
    burst = max(
        int(amount * 2 * _WS_RATE_MULTIPLIERS[period]), 2
    )  # at least 2 for heartbeat safety
    return rate, burst


class TokenBucket:
    """
    Token bucket rate limiter for WebSocket messages.

    Tokens refill continuously at the configured rate. Each message event
    consumes one token. When the bucket is empty, messages are dropped.

    Uses time.monotonic() which is immune to system clock changes.
    """

    def __init__(self, rate: float, burst: int):
        """
        Initialize token bucket.

        Args:
            rate: Token refill rate in tokens per second.
            burst: Maximum token capacity (allows short bursts).
        """
        self.rate = rate
        self.burst = burst
        self.tokens = float(burst)
        self.last_refill = time.monotonic()

    def acquire(self) -> bool:
        """
        Try to consume one token.

        Returns:
            True if token was consumed, False if rate limited.
        """
        now = time.monotonic()
        elapsed = now - self.last_refill
        self.tokens = min(float(self.burst), self.tokens + elapsed * self.rate)
        self.last_refill = now

        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True

        return False


def create_ws_message_bucket() -> TokenBucket:
    """Create a TokenBucket for WebSocket message rate limiting."""
    rate, burst = _parse_ws_rate(WS_MESSAGES_DEFAULT)
    log.debug(f"WS message rate: {rate:.2f}/s (burst: {burst})")
    return TokenBucket(rate=rate, burst=burst)


log.info(f"WS message rate limit configured: {WS_MESSAGES_DEFAULT}")
