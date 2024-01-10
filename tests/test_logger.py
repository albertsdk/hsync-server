"""
Tests for hsync_server.logger module.
"""

import logging
import pytest

from hsync_server.logger import (
    LOG_FORMAT,
    LOG_DATE_FORMAT,
    VALID_LOG_LEVELS,
    LOGGER_NAME,
    get_log_level,
    setup_logging,
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

    def test_log_date_format_iso_8601(self):
        """Test LOG_DATE_FORMAT is ISO 8601 format."""
        assert LOG_DATE_FORMAT == "%Y-%m-%dT%H:%M:%SZ"

    def test_valid_log_levels(self):
        """Test all expected log levels are defined."""
        expected = ("debug", "info", "warning", "error", "critical")
        assert VALID_LOG_LEVELS == expected

    def test_logger_name(self):
        """Test logger name constant."""
        assert LOGGER_NAME == "main"


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

    def test_logger_name(self):
        """Test logger has correct name."""
        logger = setup_logging()
        assert logger.name == LOGGER_NAME

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

    def test_uvicorn_loggers_configured(self):
        """Test uvicorn loggers are configured."""
        setup_logging()
        
        uvicorn_error = logging.getLogger("uvicorn.error")
        uvicorn_access = logging.getLogger("uvicorn.access")
        
        assert uvicorn_error.propagate is False
        assert uvicorn_access.propagate is False
        assert len(uvicorn_error.handlers) >= 1
        assert len(uvicorn_access.handlers) >= 1

