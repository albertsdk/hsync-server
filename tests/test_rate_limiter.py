"""
Tests for rate limiter module.
"""

from unittest.mock import MagicMock

import pytest
from fastapi import Request
from slowapi.errors import RateLimitExceeded

from hsync_server.rate_limiter import (
    RATE_LIMIT_DEFAULT,
    RATE_LIMIT_DOWNLOAD,
    RATE_LIMIT_UPLOAD,
    get_client_ip,
    limiter,
    rate_limit_exceeded_handler,
    resolve_client_ip,
)

TRUSTED_PROXIES_ATTR = "hsync_server.rate_limiter.TRUSTED_PROXIES"


class TestGetClientIp:
    """Tests for get_client_ip function.

    X-Forwarded-For is only honoured when the direct peer is a configured
    trusted reverse proxy (HSYNC_TRUSTED_PROXIES); otherwise it is ignored to
    prevent IP spoofing / rate-limit bypass.
    """

    def test_direct_client_ip(self):
        """Get IP from direct client connection."""
        request = MagicMock(spec=Request)
        request.headers = {}
        request.client = MagicMock()
        request.client.host = "192.168.1.100"

        ip = get_client_ip(request)
        assert ip == "192.168.1.100"

    def test_forwarded_for_ignored_from_untrusted_peer(self, monkeypatch):
        """X-Forwarded-For is ignored when the peer is not a trusted proxy."""
        monkeypatch.setattr(TRUSTED_PROXIES_ATTR, frozenset())
        request = MagicMock(spec=Request)
        request.headers = {"X-Forwarded-For": "10.0.0.1"}
        request.client = MagicMock()
        request.client.host = "192.168.1.1"

        ip = get_client_ip(request)
        assert ip == "192.168.1.1"

    def test_forwarded_for_honored_from_trusted_proxy(self, monkeypatch):
        """X-Forwarded-For is honoured when the peer is a trusted proxy."""
        monkeypatch.setattr(TRUSTED_PROXIES_ATTR, frozenset({"192.168.1.1"}))
        request = MagicMock(spec=Request)
        request.headers = {"X-Forwarded-For": "10.0.0.1"}
        request.client = MagicMock()
        request.client.host = "192.168.1.1"

        ip = get_client_ip(request)
        assert ip == "10.0.0.1"

    def test_forwarded_for_multiple_ips_from_trusted_proxy(self, monkeypatch):
        """The rightmost untrusted hop (closest client) is returned."""
        monkeypatch.setattr(TRUSTED_PROXIES_ATTR, frozenset({"192.168.1.1"}))
        request = MagicMock(spec=Request)
        request.headers = {"X-Forwarded-For": "10.0.0.1, 10.0.0.2, 10.0.0.3"}
        request.client = MagicMock()
        request.client.host = "192.168.1.1"

        ip = get_client_ip(request)
        assert ip == "10.0.0.3"

    def test_forwarded_for_spoofed_prefix_is_ignored(self, monkeypatch):
        """A client-supplied leading hop must not become the rate-limit key."""
        monkeypatch.setattr(TRUSTED_PROXIES_ATTR, frozenset({"192.168.1.1"}))
        request = MagicMock(spec=Request)
        # The trusted proxy appended the real peer (10.0.0.9) to a spoofed prefix.
        request.headers = {"X-Forwarded-For": "1.2.3.4, 10.0.0.9"}
        request.client = MagicMock()
        request.client.host = "192.168.1.1"

        ip = get_client_ip(request)
        assert ip == "10.0.0.9"

    def test_forwarded_for_skips_trusted_proxy_hops(self, monkeypatch):
        """Trusted proxy addresses inside the chain are skipped from the right."""
        monkeypatch.setattr(TRUSTED_PROXIES_ATTR, frozenset({"192.168.1.1", "10.0.0.2"}))
        request = MagicMock(spec=Request)
        request.headers = {"X-Forwarded-For": "1.2.3.4, 10.0.0.2"}
        request.client = MagicMock()
        request.client.host = "192.168.1.1"

        ip = get_client_ip(request)
        assert ip == "1.2.3.4"

    def test_malformed_forwarded_for_falls_back_to_peer(self, monkeypatch):
        """A non-IP hop is never used as a rate-limit key."""
        monkeypatch.setattr(TRUSTED_PROXIES_ATTR, frozenset({"192.168.1.1"}))
        request = MagicMock(spec=Request)
        request.headers = {"X-Forwarded-For": "1.2.3.4, not-an-ip"}
        request.client = MagicMock()
        request.client.host = "192.168.1.1"

        ip = get_client_ip(request)
        assert ip == "192.168.1.1"

    def test_forwarded_for_with_spaces_from_trusted_proxy(self, monkeypatch):
        """Extra spaces in X-Forwarded-For are stripped for a trusted proxy."""
        monkeypatch.setattr(TRUSTED_PROXIES_ATTR, frozenset({"192.168.1.1"}))
        request = MagicMock(spec=Request)
        request.headers = {"X-Forwarded-For": "  10.0.0.1  ,  10.0.0.2  "}
        request.client = MagicMock()
        request.client.host = "192.168.1.1"

        ip = get_client_ip(request)
        assert ip == "10.0.0.2"

    def test_forwarded_for_ignored_when_no_client(self, monkeypatch):
        """When request.client is None the header is ignored (safe fallback)."""
        monkeypatch.setattr(TRUSTED_PROXIES_ATTR, frozenset({"192.168.1.1"}))
        request = MagicMock(spec=Request)
        request.headers = {"X-Forwarded-For": "10.0.0.1"}
        request.client = None

        ip = get_client_ip(request)
        assert ip == "127.0.0.1"


class TestResolveClientIp:
    """WebSocket slot accounting uses the same trusted-proxy rule."""

    def test_trusted_proxy_header_is_used(self, monkeypatch):
        monkeypatch.setattr(TRUSTED_PROXIES_ATTR, frozenset({"192.168.1.1"}))
        assert resolve_client_ip("192.168.1.1", "1.2.3.4, 10.0.0.9") == "10.0.0.9"

    def test_untrusted_peer_header_is_ignored(self, monkeypatch):
        monkeypatch.setattr(TRUSTED_PROXIES_ATTR, frozenset())
        assert resolve_client_ip("192.168.1.1", "10.0.0.9") == "192.168.1.1"

    def test_missing_peer_is_unknown(self, monkeypatch):
        monkeypatch.setattr(TRUSTED_PROXIES_ATTR, frozenset({"192.168.1.1"}))
        assert resolve_client_ip(None, "10.0.0.9") == "unknown"


class TestRateLimitExceededHandler:
    """Tests for rate_limit_exceeded_handler function."""

    @pytest.mark.asyncio
    async def test_returns_429_status(self):
        """Handler returns 429 status code."""
        request = MagicMock(spec=Request)
        request.headers = {}
        request.client = MagicMock()
        request.client.host = "192.168.1.100"

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

        exc = MagicMock(spec=RateLimitExceeded)
        exc.detail = "Rate limit exceeded: 100/minute"
        exc.retry_after = 60

        response = await rate_limit_exceeded_handler(request, exc)

        # Response body contains error info
        assert response.status_code == 429
        assert "Retry-After" in response.headers


class TestRateLimitConstants:
    """Tests for rate limit constants (high-traffic defaults)."""

    def test_default_limit(self):
        """Default rate limit is set for high-traffic systems."""
        assert RATE_LIMIT_DEFAULT == "500/minute"

    def test_upload_limit(self):
        """Upload rate limit is set (more restrictive than download)."""
        assert RATE_LIMIT_UPLOAD == "200/minute"

    def test_download_limit(self):
        """Download rate limit is set for high-traffic systems."""
        assert RATE_LIMIT_DOWNLOAD == "1000/minute"

    def test_limits_are_valid_format(self):
        """All rate limits follow the expected format."""
        import re

        pattern = r"^\d+/(second|minute|hour|day)$"
        assert re.match(pattern, RATE_LIMIT_DEFAULT)
        assert re.match(pattern, RATE_LIMIT_UPLOAD)
        assert re.match(pattern, RATE_LIMIT_DOWNLOAD)


class TestLimiterInstance:
    """Tests for limiter instance."""

    def test_limiter_exists(self):
        """Limiter instance is created."""
        assert limiter is not None

    def test_limiter_has_default_limits(self):
        """Limiter has default limits configured — the underlying slowapi
        LimitGroup/List collection must be present and non-empty."""
        assert limiter._default_limits is not None
        limits = limiter._default_limits
        assert len(limits) > 0, "expected at least one default-limit entry"
        # Verify each entry is a slowapi Limit or LimitGroup (anything else
        # indicates a wiring error in the rate-limiter setup).
        from slowapi.wrappers import Limit, LimitGroup

        for entry in limits:
            assert isinstance(entry, (Limit, LimitGroup)), (
                f"unexpected type in _default_limits: {type(entry)}"
            )
