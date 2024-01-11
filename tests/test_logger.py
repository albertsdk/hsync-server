"""
Tests for hsync_server.logger module.
"""

import json
import logging
import os
import pytest

from hsync_server.logger import (
    LOG_FORMAT,
    LOG_DATE_FORMAT,
    VALID_LOG_LEVELS,
    VALID_LOG_FORMATS,
    HOSTNAME,
    get_log_level,
    get_log_format,
    get_request_id,
    set_request_id,
    setup_logging,
    RequestIdFilter,
    JsonFormatter,
)


class TestLogConstants:
    """Tests for logging constants."""

    def test_log_format_contains_required_fields(self):
        """Test LOG_FORMAT contains required formatting fields."""
        assert "%(asctime)s" in LOG_FORMAT
        assert "%(msecs)03d" in LOG_FORMAT
        assert "%(process)d" in LOG_FORMAT
        assert "%(levelname)s" in LOG_FORMAT
        assert "%(message)s" in LOG_FORMAT
        assert "%(request_id)s" in LOG_FORMAT

    def test_log_date_format_iso_8601(self):
        """Test LOG_DATE_FORMAT is ISO 8601 format."""
        assert LOG_DATE_FORMAT == "%Y-%m-%dT%H:%M:%SZ"

    def test_valid_log_levels(self):
        """Test all expected log levels are defined."""
        expected = ("debug", "info", "warning", "error", "critical")
        assert VALID_LOG_LEVELS == expected

    def test_valid_log_formats(self):
        """Test valid log formats."""
        assert VALID_LOG_FORMATS == ("text", "json")

    def test_hostname_is_string(self):
        """Test HOSTNAME is a non-empty string."""
        assert isinstance(HOSTNAME, str)
        assert len(HOSTNAME) > 0


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


class TestRequestIdFilter:
    """Tests for RequestIdFilter class."""

    def test_filter_adds_request_id(self):
        """Test filter adds request_id to log record."""
        set_request_id("filter-test-id")
        filter_obj = RequestIdFilter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="test message", args=(), exc_info=None
        )
        result = filter_obj.filter(record)
        assert result is True
        assert record.request_id == "filter-test-id"
        set_request_id(None)  # cleanup

    def test_filter_uses_dash_when_no_request_id(self):
        """Test filter uses '-' when no request ID is set."""
        set_request_id(None)
        filter_obj = RequestIdFilter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="test message", args=(), exc_info=None
        )
        filter_obj.filter(record)
        assert record.request_id == "-"


class TestJsonFormatter:
    """Tests for JsonFormatter class."""

    def test_format_returns_valid_json(self):
        """Test formatter returns valid JSON."""
        formatter = JsonFormatter()
        record = logging.LogRecord(
            name="test.logger", level=logging.INFO, pathname="test.py",
            lineno=10, msg="test message", args=(), exc_info=None
        )
        result = formatter.format(record)
        parsed = json.loads(result)
        assert "timestamp" in parsed
        assert parsed["level"] == "INFO"
        assert parsed["message"] == "test message"
        assert "pid" in parsed
        assert "hostname" in parsed

    def test_format_includes_request_id_when_set(self):
        """Test formatter includes request_id when set."""
        set_request_id("json-test-id")
        formatter = JsonFormatter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="test", args=(), exc_info=None
        )
        result = formatter.format(record)
        parsed = json.loads(result)
        assert parsed["request_id"] == "json-test-id"
        set_request_id(None)  # cleanup

    def test_format_excludes_request_id_when_not_set(self):
        """Test formatter excludes request_id when not set."""
        set_request_id(None)
        formatter = JsonFormatter()
        record = logging.LogRecord(
            name="hsync", level=logging.INFO, pathname="", lineno=0,
            msg="test", args=(), exc_info=None
        )
        result = formatter.format(record)
        parsed = json.loads(result)
        assert "request_id" not in parsed


class TestGetLogFormat:
    """Tests for get_log_format function."""

    def test_default_is_text(self):
        """Test default format is 'text'."""
        # Ensure env var is not set
        os.environ.pop("HSYNC_LOG_FORMAT", None)
        assert get_log_format() == "text"

    def test_json_format_from_env(self):
        """Test JSON format from environment variable."""
        os.environ["HSYNC_LOG_FORMAT"] = "json"
        assert get_log_format() == "json"
        os.environ.pop("HSYNC_LOG_FORMAT", None)

    def test_invalid_format_defaults_to_text(self):
        """Test invalid format defaults to 'text'."""
        os.environ["HSYNC_LOG_FORMAT"] = "invalid"
        assert get_log_format() == "text"
        os.environ.pop("HSYNC_LOG_FORMAT", None)


class TestGetLogLevel:
    """Tests for get_log_level function."""

    def test_default_is_info(self):
        """Test None defaults to INFO level."""
        assert get_log_level(None) == logging.INFO

    def test_debug_level(self):
        """Test debug level."""
        assert get_log_level("debug") == logging.DEBUG

    def test_info_level(self):
        """Test info level."""
        assert get_log_level("info") == logging.INFO

    def test_warning_level(self):
        """Test warning level."""
        assert get_log_level("warning") == logging.WARNING

    def test_error_level(self):
        """Test error level."""
        assert get_log_level("error") == logging.ERROR

    def test_critical_level(self):
        """Test critical level."""
        assert get_log_level("critical") == logging.CRITICAL

    def test_case_insensitive(self):
        """Test level is case-insensitive."""
        assert get_log_level("DEBUG") == logging.DEBUG
        assert get_log_level("Info") == logging.INFO
        assert get_log_level("WARNING") == logging.WARNING

    def test_invalid_level_raises(self):
        """Test invalid level raises ValueError."""
        with pytest.raises(ValueError) as exc_info:
            get_log_level("invalid")
        assert "Invalid log level: 'invalid'" in str(exc_info.value)
        assert "debug" in str(exc_info.value)
        assert "info" in str(exc_info.value)


class TestSetupLogging:
    """Tests for setup_logging function."""

    def test_returns_logger(self):
        """Test setup_logging returns a logger."""
        logger = setup_logging()
        assert isinstance(logger, logging.Logger)

    def test_logger_name_is_hsync_server(self):
        """Test logger has correct name for module hierarchy."""
        logger = setup_logging()
        assert logger.name == "hsync_server"

    def test_default_level_is_info(self):
        """Test default level is INFO."""
        logger = setup_logging()
        assert logger.level == logging.INFO

    def test_debug_level(self):
        """Test debug level configuration."""
        logger = setup_logging(level="debug")
        assert logger.level == logging.DEBUG

    def test_has_handler(self):
        """Test logger has at least one handler."""
        logger = setup_logging()
        assert len(logger.handlers) >= 1

    def test_handler_has_formatter(self):
        """Test handler has formatter configured."""
        logger = setup_logging()
        handler = logger.handlers[0]
        assert handler.formatter is not None

    def test_handler_has_request_id_filter(self):
        """Test handler has RequestIdFilter configured."""
        logger = setup_logging()
        handler = logger.handlers[0]
        filter_types = [type(f).__name__ for f in handler.filters]
        assert "RequestIdFilter" in filter_types

    def test_no_propagation(self):
        """Test logger does not propagate."""
        logger = setup_logging()
        assert logger.propagate is False

    def test_clears_existing_handlers(self):
        """Test setup clears existing handlers."""
        logger = setup_logging()
        initial_count = len(logger.handlers)

        # Setup again
        logger = setup_logging()

        # Should have same number of handlers (cleared and re-added)
        assert len(logger.handlers) == initial_count

    def test_json_format(self):
        """Test JSON format configuration."""
        logger = setup_logging(log_format="json")
        handler = logger.handlers[0]
        assert isinstance(handler.formatter, JsonFormatter)

    def test_text_format_default(self):
        """Test text format is default."""
        logger = setup_logging(log_format="text")
        handler = logger.handlers[0]
        assert not isinstance(handler.formatter, JsonFormatter)

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

