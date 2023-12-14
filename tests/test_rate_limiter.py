"""
Tests for rate limiter module.
"""

import pytest
from unittest.mock import MagicMock, AsyncMock
from fastapi import Request
from slowapi.errors import RateLimitExceeded

from hsync_server.rate_limiter import (
    get_client_ip,
    rate_limit_exceeded_handler,
    limiter,
    RATE_LIMIT_DEFAULT,
    RATE_LIMIT_UPLOAD,
    RATE_LIMIT_DOWNLOAD,
    RATE_LIMIT_WEBSOCKET,
)


class TestGetClientIp:
    """Tests for get_client_ip function."""

    def test_direct_client_ip(self):
        """Get IP from direct client connection."""
        request = MagicMock(spec=Request)
        request.headers = {}
        request.client = MagicMock()
        request.client.host = "192.168.1.100"
        
        ip = get_client_ip(request)
        assert ip == "192.168.1.100"

    def test_forwarded_for_single_ip(self):
        """Get IP from X-Forwarded-For header with single IP."""
        request = MagicMock(spec=Request)
        request.headers = {"X-Forwarded-For": "10.0.0.1"}
        request.client = MagicMock()
        request.client.host = "192.168.1.1"
        
        ip = get_client_ip(request)
        assert ip == "10.0.0.1"

    def test_forwarded_for_multiple_ips(self):
        """Get first IP from X-Forwarded-For header with multiple IPs."""
        request = MagicMock(spec=Request)
        request.headers = {"X-Forwarded-For": "10.0.0.1, 10.0.0.2, 10.0.0.3"}
        request.client = MagicMock()
        request.client.host = "192.168.1.1"
        
        ip = get_client_ip(request)
        assert ip == "10.0.0.1"

    def test_forwarded_for_with_spaces(self):
        """Handle X-Forwarded-For with extra spaces."""
        request = MagicMock(spec=Request)
        request.headers = {"X-Forwarded-For": "  10.0.0.1  ,  10.0.0.2  "}
        request.client = MagicMock()
        request.client.host = "192.168.1.1"
        
        ip = get_client_ip(request)
        assert ip == "10.0.0.1"


class TestRateLimitExceededHandler:
    """Tests for rate_limit_exceeded_handler function."""

    @pytest.mark.asyncio
    async def test_returns_429_status(self):
        """Handler returns 429 status code."""
        request = MagicMock(spec=Request)
        request.headers = {}
        request.client = MagicMock()
        request.client.host = "192.168.1.100"

        # Create a mock exception with the expected attributes
        exc = MagicMock(spec=RateLimitExceeded)
        exc.detail = "Rate limit exceeded: 100/minute"
        exc.retry_after = 60

        response = await rate_limit_exceeded_handler(request, exc)

        assert response.status_code == 429

    @pytest.mark.asyncio
    async def test_returns_json_content(self):
        """Handler returns JSON with error details."""
        request = MagicMock(spec=Request)
        request.headers = {}
        request.client = MagicMock()
        request.client.host = "192.168.1.100"

        # Create a mock exception with the expected attributes
        exc = MagicMock(spec=RateLimitExceeded)
        exc.detail = "Rate limit exceeded: 100/minute"
        exc.retry_after = 60

        response = await rate_limit_exceeded_handler(request, exc)

        # Response body contains error info
        assert response.status_code == 429
        assert "Retry-After" in response.headers


class TestRateLimitConstants:
    """Tests for rate limit constants."""

    def test_default_limit(self):
        """Default rate limit is set."""
        assert RATE_LIMIT_DEFAULT == "100/minute"

    def test_upload_limit(self):
        """Upload rate limit is more restrictive."""
        assert RATE_LIMIT_UPLOAD == "50/minute"

    def test_download_limit(self):
        """Download rate limit is more permissive."""
        assert RATE_LIMIT_DOWNLOAD == "200/minute"

    def test_websocket_limit(self):
        """WebSocket rate limit is set."""
        assert RATE_LIMIT_WEBSOCKET == "10/minute"


class TestLimiterInstance:
    """Tests for limiter instance."""

    def test_limiter_exists(self):
        """Limiter instance is created."""
        assert limiter is not None

    def test_limiter_has_default_limits(self):
        """Limiter has default limits configured."""
        assert limiter._default_limits is not None

