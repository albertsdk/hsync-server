# HSYNC Server - HTTP-based file synchronization server
# Copyright (C) 2025 Alberto Basso <alberto.sdk [at] gmail [dot] com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

"""
Logging configuration for HSYNC Server.

This module provides a standardized logging setup with enterprise-grade features:
- ISO 8601 timestamps with milliseconds
- Request ID correlation for distributed tracing
- Optional JSON structured logging for log aggregation systems
- Configurable log levels

Log formats:
- Text: 2024-01-15T10:30:45.123Z - [12345] [req-abc123] INFO: message
- JSON: {"timestamp": "...", "level": "INFO", "pid": 12345, "request_id": "...", ...}
"""

import json
import logging
import os
import socket
import sys
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Final, Optional


# Context variable for request ID - thread-safe storage for per-request data
request_id_var: ContextVar[Optional[str]] = ContextVar("request_id", default=None)

# Log format matching client style (ISO 8601 timestamp with milliseconds)
LOG_FORMAT: Final[str] = (
    "%(asctime)s.%(msecs)03d - [%(process)d] [%(request_id)s] %(levelname)s: %(message)s"
)
LOG_FORMAT_NO_REQUEST_ID: Final[str] = (
    "%(asctime)s.%(msecs)03d - [%(process)d] %(levelname)s: %(message)s"
)
LOG_DATE_FORMAT: Final[str] = "%Y-%m-%dT%H:%M:%SZ"

# Valid log levels (Python standard logging levels)
VALID_LOG_LEVELS: Final[tuple[str, ...]] = (
    "debug",
    "info",
    "warning",
    "error",
    "critical",
)

# Valid log formats
VALID_LOG_FORMATS: Final[tuple[str, ...]] = ("text", "json")

# Hostname for JSON logs
HOSTNAME: Final[str] = socket.gethostname()


def get_request_id() -> Optional[str]:
    """Get the current request ID from context."""
    return request_id_var.get()


def set_request_id(request_id: Optional[str]) -> None:
    """Set the request ID in context for the current request."""
    request_id_var.set(request_id)


class RequestIdFilter(logging.Filter):
    """
    Logging filter that adds request_id to log records.

    This filter retrieves the request ID from the context variable
    and adds it to each log record, enabling request correlation.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Add request_id to the log record."""
        record.request_id = get_request_id() or "N/A"
        return True


class JsonFormatter(logging.Formatter):
    """
    JSON formatter for structured logging.

    Produces machine-readable JSON logs suitable for log aggregation
    systems like ELK, Loki, Datadog, or Splunk.
    """

    def format(self, record: logging.LogRecord) -> str:
        """Format the log record as JSON."""
        # Build the log entry dictionary
        log_entry: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%S.%f"
            )[:-3] + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "pid": record.process,
            "hostname": HOSTNAME,
        }

        # Add request ID if available
        request_id = get_request_id()
        if request_id:
            log_entry["request_id"] = request_id

        # Add module information
        if record.name != "hsync":
            log_entry["module"] = record.module
            log_entry["function"] = record.funcName
            log_entry["line"] = record.lineno

        # Add exception info if present
        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)

        # Add extra fields if present
        for key, value in record.__dict__.items():
            if key not in (
                "name", "msg", "args", "created", "filename", "funcName",
                "levelname", "levelno", "lineno", "module", "msecs",
                "pathname", "process", "processName", "relativeCreated",
                "stack_info", "exc_info", "exc_text", "thread", "threadName",
                "request_id", "message", "asctime",
            ):
                log_entry[key] = value

        return json.dumps(log_entry, default=str)


def get_log_level(level: Optional[str] = None) -> int:
    """
    Convert a log level string to its numeric value.

    Args:
        level: Log level string (debug, info, warning, error, critical).
               If None, defaults to "info".

    Returns:
        Numeric log level value.

    Raises:
        ValueError: If the log level is not valid.
    """
    if level is None:
        level = "info"

    level = level.lower()
    if level not in VALID_LOG_LEVELS:
        valid = ", ".join(VALID_LOG_LEVELS)
        raise ValueError(
            f"Invalid log level: '{level}'. Must be one of: {valid}"
        )

    return getattr(logging, level.upper())


def get_log_format() -> str:
    """
    Get the log format from environment variable.

    Returns:
        Log format string ("text" or "json").
    """
    fmt = os.environ.get("HSYNC_LOG_FORMAT", "text").lower()
    if fmt not in VALID_LOG_FORMATS:
        return "text"
    return fmt


def setup_logging(level: str = "info", log_format: Optional[str] = None) -> logging.Logger:
    """
    Configure and return the HSYNC server logger.

    Sets up logging with enterprise-grade features including request ID
    correlation and optional JSON structured output.

    Args:
        level: Log level string (debug, info, warning, error, critical).
        log_format: Output format ("text" or "json"). If None, reads from
                   HSYNC_LOG_FORMAT environment variable.

    Returns:
        Configured logger instance.
    """
    # Get numeric log level
    log_level = get_log_level(level)

    # Determine log format
    if log_format is None:
        log_format = get_log_format()

    # Create formatter based on format type
    if log_format == "json":
        formatter: logging.Formatter = JsonFormatter()
    else:
        formatter = logging.Formatter(fmt=LOG_FORMAT, datefmt=LOG_DATE_FORMAT)

    # Create console handler with request ID filter
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(log_level)
    console_handler.addFilter(RequestIdFilter())
    console_handler.setFormatter(formatter)

    # Configure the hsync_server logger (parent of all module loggers like hsync_server.api.files)
    # This allows getLogger(__name__) in modules to inherit this configuration
    hsync_server_logger = logging.getLogger("hsync_server")
    hsync_server_logger.setLevel(log_level)
    hsync_server_logger.handlers.clear()
    hsync_server_logger.addHandler(console_handler)
    hsync_server_logger.propagate = False

    # Also configure the "hsync" logger for backward compatibility
    hsync_logger = logging.getLogger("hsync")
    hsync_logger.setLevel(log_level)
    hsync_logger.handlers.clear()
    hsync_handler = logging.StreamHandler(sys.stdout)
    hsync_handler.setLevel(log_level)
    hsync_handler.addFilter(RequestIdFilter())
    hsync_handler.setFormatter(formatter)
    hsync_logger.addHandler(hsync_handler)
    hsync_logger.propagate = False

    # Configure uvicorn loggers to use the same format
    for uvicorn_logger_name in ("uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(uvicorn_logger_name)
        uvicorn_logger.handlers.clear()
        uvicorn_handler = logging.StreamHandler(sys.stdout)
        uvicorn_handler.setLevel(log_level)
        uvicorn_handler.addFilter(RequestIdFilter())
        uvicorn_handler.setFormatter(formatter)
        uvicorn_logger.addHandler(uvicorn_handler)
        uvicorn_logger.propagate = False

    return hsync_server_logger

