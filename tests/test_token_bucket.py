"""
Tests for WebSocket TokenBucket rate limiter.
"""

import re
import time
from unittest.mock import patch

import pytest

from hsync_server.rate_limiter import (
    WS_MESSAGES_DEFAULT,
    TokenBucket,
    _parse_ws_rate,
    create_ws_message_bucket,
)


class TestParseWsRate:
    """Tests for _parse_ws_rate function."""

    def test_second_rate(self):
        """Parse "10/second" rate string."""
        rate, burst = _parse_ws_rate("10/second")
        assert rate == 10.0
        assert burst == 20

    def test_minute_rate(self):
        """Parse "60/minute" rate string."""
        rate, burst = _parse_ws_rate("60/minute")
        assert rate == 1.0  # 60/min = 1/sec
        assert burst == 2

    def test_hour_rate(self):
        """Parse "3600/hour" rate string."""
        rate, burst = _parse_ws_rate("3600/hour")
        assert rate == 1.0  # 3600/hour = 1/sec
        assert burst == 2

    def test_day_rate(self):
        """Parse "86400/day" rate string."""
        rate, burst = _parse_ws_rate("86400/day")
        assert rate == 1.0  # 86400/day = 1/sec
        assert burst == 2

    def test_large_burst(self):
        """Large rate gives proportional burst."""
        rate, burst = _parse_ws_rate("1000/second")
        assert rate == 1000.0
        assert burst == 2000

    def test_burst_minimum_two(self):
        """Burst is never less than 2."""
        rate, burst = _parse_ws_rate("1/minute")
        assert burst == 2

    def test_invalid_format_returns_default(self):
        """Invalid format falls back to default 10/s, burst 20."""
        rate, burst = _parse_ws_rate("not-a-valid-rate")
        assert rate == 10.0
        assert burst == 20


class TestTokenBucket:
    """Tests for TokenBucket class."""

    def test_initial_state(self):
        """Bucket starts with full burst capacity."""
        bucket = TokenBucket(rate=10.0, burst=20)
        assert bucket.tokens == 20.0
        assert bucket.rate == 10.0
        assert bucket.burst == 20
        assert bucket.total_dropped == 0

    def test_acquire_single_token(self):
        """Single acquire consumes one token."""
        bucket = TokenBucket(rate=10.0, burst=20)
        assert bucket.acquire() is True
        assert bucket.tokens == 19.0

    def test_acquire_exhaust_burst(self):
        """Acquiring all tokens exhausts the bucket."""
        bucket = TokenBucket(rate=10.0, burst=5)
        for _ in range(5):
            assert bucket.acquire() is True
        # 6th acquire should fail
        assert bucket.acquire() is False
        assert bucket.total_dropped == 1

    def test_token_refill_over_time(self):
        """Tokens refill at the configured rate."""
        bucket = TokenBucket(rate=100.0, burst=10)  # 100 tokens/sec
        # Exhaust the bucket
        for _ in range(10):
            assert bucket.acquire() is True
        assert bucket.acquire() is False

        # Simulate 0.1 seconds passing (should refill 10 tokens)
        with patch.object(time, "monotonic", return_value=bucket.last_refill + 0.1):
            # Should be able to acquire 10 tokens again
            for _ in range(10):
                assert bucket.acquire() is True
            assert bucket.acquire() is False

    def test_tokens_dont_exceed_burst(self):
        """Tokens never exceed burst capacity."""
        bucket = TokenBucket(rate=100.0, burst=10)
        # Simulate long idle period
        with patch.object(time, "monotonic", return_value=bucket.last_refill + 10.0):
            assert bucket.acquire() is True
            assert bucket.tokens == 9.0  # burst=10, consumed 1

    def test_total_dropped_counter(self):
        """total_dropped increments on each failed acquire."""
        bucket = TokenBucket(rate=1.0, burst=1)
        bucket.acquire()  # consume token
        assert bucket.total_dropped == 0
        bucket.acquire()  # fail
        assert bucket.total_dropped == 1
        bucket.acquire()  # fail again
        assert bucket.total_dropped == 2

    def test_acquire_with_no_refill_time(self):
        """Acquire fails immediately after exhausting with no time elapsed."""
        bucket = TokenBucket(rate=0.1, burst=1)
        bucket.acquire()  # consume the only token
        # No time passed, same monotonic value
        with patch.object(time, "monotonic", return_value=bucket.last_refill):
            assert bucket.acquire() is False


class TestCreateWsMessageBucket:
    """Tests for create_ws_message_bucket factory."""

    def test_default_bucket(self):
        """create_ws_message_bucket uses default env var."""
        bucket = create_ws_message_bucket()
        assert isinstance(bucket, TokenBucket)
        assert bucket.rate == 10.0  # default 10/second
        assert bucket.burst == 20

    def test_custom_env_var(self):
        """create_ws_message_bucket respects HSYNC_RATE_LIMIT_WS_MESSAGES."""
        with patch.dict("os.environ", {"HSYNC_RATE_LIMIT_WS_MESSAGES": "5/second"}):
            # Re-import to pick up env var (module-level constant)
            from importlib import reload

            import hsync_server.rate_limiter
            from hsync_server import config

            try:
                config.get_settings.cache_clear()
                reload(hsync_server.rate_limiter)
                bucket = hsync_server.rate_limiter.create_ws_message_bucket()
                assert bucket.rate == 5.0
                assert bucket.burst == 10
            finally:
                config.get_settings.cache_clear()
                reload(hsync_server.rate_limiter)


class TestWsMessagesDefault:
    """Tests for WS_MESSAGES_DEFAULT constant."""

    def test_default_value(self):
        """Default WS message rate is 10/second."""
        assert WS_MESSAGES_DEFAULT == "10/second"

    def test_valid_format(self):
        """WS_MESSAGES_DEFAULT follows expected format."""
        pattern = r"^\d+/(second|minute|hour|day)$"
        assert re.match(pattern, WS_MESSAGES_DEFAULT) is not None


class TestTokenBucketConcurrency:
    """Tests for concurrent access patterns."""

    def test_rapid_acquire_release(self):
        """Rapid acquire/release cycles simulate real traffic."""
        bucket = TokenBucket(rate=1000.0, burst=100)

        # Acquire 80 tokens (leave approximately 20)
        for _ in range(80):
            assert bucket.acquire() is True
        assert bucket.tokens == pytest.approx(20.0, abs=1.0)

        # Simulate 0.01s of refill (10 tokens)
        with patch.object(time, "monotonic", return_value=bucket.last_refill + 0.01):
            for _ in range(30):
                assert bucket.acquire() is True
            # Should be empty now
            assert bucket.acquire() is False

    def test_burst_recovery(self):
        """Bucket recovers fully after idle period."""
        bucket = TokenBucket(rate=10.0, burst=10)

        # Exhaust
        for _ in range(10):
            assert bucket.acquire() is True

        # Idle 2 seconds (20 tokens refill, capped at burst 10)
        with patch.object(time, "monotonic", return_value=bucket.last_refill + 2.0):
            for _ in range(10):
                assert bucket.acquire() is True
            assert bucket.acquire() is False
