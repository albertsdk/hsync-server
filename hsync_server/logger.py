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
Logging configuration for HSYNC Server using Loguru.

This module provides a standardized logging setup with enterprise-grade features:
- ISO 8601 timestamps with milliseconds
- Request ID correlation for distributed tracing
- Optional JSON structured logging for log aggregation systems
- Configurable log levels
- Unified logging for uvicorn and all application logs

Log formats:
- Text: 2024-01-15T10:30:45.123Z - [12345] [req-abc123] INFO: message
- JSON: {"timestamp": "...", "level": "INFO", "pid": 12345, "request_id": "...", ...}
"""

import logging
import os
import sys
from contextvars import ContextVar
from typing import Final, Optional

from loguru import logger


# Context variable for request ID - thread-safe storage for per-request data
request_id_var: ContextVar[Optional[str]] = ContextVar("request_id", default=None)

# Valid log levels - imported from config module to avoid circular imports
# Re-exported here for backward compatibility
from hsync_server.config import VALID_LOG_LEVELS

# Valid log formats
VALID_LOG_FORMATS: Final[tuple[str, ...]] = ("text", "json")


def get_request_id() -> Optional[str]:
    """Get the current request ID from context."""
    return request_id_var.get()


def set_request_id(request_id: Optional[str]) -> None:
    """Set the request ID in context for the current request."""
    request_id_var.set(request_id)


class InterceptHandler(logging.Handler):
    """
    Handler that intercepts standard logging and redirects to loguru.

    This allows uvicorn and other libraries using standard logging
    to have their logs processed through loguru with consistent formatting.
    """

    def emit(self, record: logging.LogRecord) -> None:
        """Redirect log record to loguru."""
        # Get corresponding loguru level
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        # Find caller from where originated the logged message
        frame, depth = logging.currentframe(), 2
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1

        # Bind request_id to the log context
        request_id = get_request_id()
        log = logger.bind(request_id=request_id or "N/A")
        log.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


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


def _format_record(record: dict) -> str:
    """
    Custom format function for loguru.

    Produces: 2024-01-15T10:30:45.123Z - [12345] [req-abc123] INFO: message
    """
    request_id = record["extra"].get("request_id", "N/A")
    timestamp = record["time"].strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    return (
        f"{timestamp} - [{record['process'].id}] [{request_id}] "
        f"{record['level'].name}: {record['message']}\n"
    )


def setup_logging(level: str = "info", log_format: Optional[str] = None) -> "logger":  # type: ignore[valid-type]
    """
    Configure and return the HSYNC server logger.

    Sets up logging with enterprise-grade features including request ID
    correlation and optional JSON structured output.

    Args:
        level: Log level string (debug, info, warning, error, critical).
        log_format: Output format ("text" or "json"). If None, reads from
                   HSYNC_LOG_FORMAT environment variable.

    Returns:
        Configured loguru logger instance.
    """
    # Validate log level
    level_lower = level.lower()
    if level_lower not in VALID_LOG_LEVELS:
        valid = ", ".join(VALID_LOG_LEVELS)
        raise ValueError(f"Invalid log level: '{level}'. Must be one of: {valid}")

    # Determine log format
    if log_format is None:
        log_format = get_log_format()

    # Remove default loguru handler
    logger.remove()

    # Add new handler with custom format
    if log_format == "json":
        logger.add(
            sys.stdout,
            level=level.upper(),
            format="{message}",
            serialize=True,  # JSON output
        )
    else:
        logger.add(
            sys.stdout,
            level=level.upper(),
            format=_format_record,
            colorize=False,
        )

    # Intercept standard logging (for uvicorn and other libraries)
    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)

    # Configure specific loggers to use intercept handler
    for logger_name in (
        "uvicorn",
        "uvicorn.error",
        "uvicorn.access",
        "hsync",
        "hsync_server",
    ):
        logging.getLogger(logger_name).handlers = [InterceptHandler()]
        logging.getLogger(logger_name).propagate = False

    return logger

