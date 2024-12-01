"""
Tests for hsync_server.logger module.

Tests the loguru-based logging configuration.
"""

import logging
import os

import pytest

from hsync_server import config
from hsync_server.logger import (
    VALID_LOG_FORMATS,
    VALID_LOG_LEVELS,
    InterceptHandler,
    get_log_format,
    get_request_id,
    set_request_id,
    setup_logging,
)


class TestLogConstants:
    """Tests for logging constants."""

    def test_valid_log_levels(self):
        """Test all expected log levels are defined."""
        expected = ("debug", "info", "warning", "error", "critical")
        assert expected == VALID_LOG_LEVELS

    def test_valid_log_formats(self):
        """Test valid log formats."""
        assert VALID_LOG_FORMATS == ("text", "json")


class TestRequestIdContext:
    """Tests for request ID context functions."""

    def test_get_request_id_default_none(self):
        """Test get_request_id returns None by default."""
        set_request_id(None)
        assert get_request_id() is None

    def test_set_and_get_request_id(self):
        """Test setting and getting request ID."""
        set_request_id("test-request-123")
        assert get_request_id() == "test-request-123"
        set_request_id(None)  # cleanup

    def test_request_id_can_be_cleared(self):
        """Test request ID can be cleared."""
        set_request_id("test-id")
        set_request_id(None)
        assert get_request_id() is None


class TestInterceptHandler:
    """Tests for InterceptHandler class."""

    def test_handler_is_logging_handler(self):
        """Test InterceptHandler is a logging.Handler."""
        handler = InterceptHandler()
        assert isinstance(handler, logging.Handler)


class TestGetLogFormat:
    """Tests for get_log_format function."""

    def test_default_is_text(self, monkeypatch):
        """Test default format is 'text'."""
        monkeypatch.delenv("HSYNC_LOG_FORMAT", raising=False)
        config.get_settings.cache_clear()
        try:
            assert get_log_format() == "text"
        finally:
            config.get_settings.cache_clear()

    def test_json_format_from_env(self, monkeypatch):
        """Test JSON format from environment variable."""
        monkeypatch.setenv("HSYNC_LOG_FORMAT", "json")
        config.get_settings.cache_clear()
        try:
            assert get_log_format() == "json"
        finally:
            config.get_settings.cache_clear()

    def test_invalid_format_defaults_to_text(self, monkeypatch):
        """Test invalid format defaults to 'text'."""
        monkeypatch.setenv("HSYNC_LOG_FORMAT", "invalid")
        config.get_settings.cache_clear()
        try:
            assert get_log_format() == "text"
        finally:
            config.get_settings.cache_clear()


class TestSetupLogging:
    """Tests for setup_logging function."""

    def test_returns_loguru_logger(self):
        """Test setup_logging returns loguru logger."""
        result = setup_logging()
        # Loguru logger has specific attributes
        assert hasattr(result, "info")
        assert hasattr(result, "debug")
        assert hasattr(result, "warning")
        assert hasattr(result, "error")
        assert hasattr(result, "bind")

    def test_accepts_level_parameter(self):
        """Test setup_logging accepts level parameter."""
        # Should not raise
        setup_logging(level="debug")
        setup_logging(level="info")
        setup_logging(level="warning")
        setup_logging(level="error")

    def test_invalid_level_raises(self):
        """Test invalid level raises ValueError."""
        with pytest.raises(ValueError) as exc_info:
            setup_logging(level="invalid")
        assert "Invalid log level" in str(exc_info.value)

    def test_accepts_json_format(self):
        """Test setup_logging accepts json format."""
        # Should not raise
        setup_logging(log_format="json")

    def test_accepts_text_format(self):
        """Test setup_logging accepts text format."""
        # Should not raise
        setup_logging(log_format="text")

    def test_uvicorn_loggers_configured(self):
        """Test uvicorn loggers are configured."""
        setup_logging()

        uvicorn_error = logging.getLogger("uvicorn.error")
        uvicorn_access = logging.getLogger("uvicorn.access")

        assert uvicorn_error.propagate is False
        assert uvicorn_access.propagate is False
        assert len(uvicorn_error.handlers) >= 1
        assert len(uvicorn_access.handlers) >= 1

    def test_hsync_logger_also_configured(self):
        """Test 'hsync' logger is also configured for backward compatibility."""
        setup_logging()
        hsync_logger = logging.getLogger("hsync")
        assert hsync_logger.propagate is False
        assert len(hsync_logger.handlers) >= 1
