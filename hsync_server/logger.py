# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
Logging configuration for HSYNC Server using Loguru.

This module provides a standardized logging setup with enterprise-grade features:
- ISO 8601 timestamps with milliseconds
- Request ID correlation for distributed tracing
- Colored console output following best practices:
  - DEBUG: dim/gray for low-priority messages
  - INFO: default color for normal operations
  - WARNING: yellow for attention-needed situations
  - ERROR: red for failures
  - CRITICAL: bold red for severe issues
- Optional JSON structured logging for log aggregation systems
- Configurable log levels
- Unified logging for uvicorn and all application logs
- Automatic color detection (disabled for non-TTY)
- File output support with log rotation

Log rotation configuration (via environment variables):
- HSYNC_LOG_FILE: Path to log file (enables file logging)
- HSYNC_LOG_ROTATION: When to rotate (e.g., "10 MB", "1 day", "00:00")
- HSYNC_LOG_RETENTION: How long to keep old logs (e.g., "7 days", "5")
- HSYNC_LOG_COMPRESSION: Compression format (e.g., "gz", "zip", "bz2")

Log formats:
- Text: 2024-01-15T10:30:45.123Z - [12345] [req-abc123] INFO: message
- JSON: {"timestamp": "...", "level": "INFO", "pid": 12345, "request_id": "...", ...}
"""

import logging
import os
import sys
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, Final

from hsync_common.logger import (
    DEFAULT_LOG_COMPRESSION,
    DEFAULT_LOG_RETENTION,
    DEFAULT_LOG_ROTATION,
    LEVEL_STYLES,
    BaseInterceptHandler,
    get_rotation_config,
)
from loguru import logger

if TYPE_CHECKING:
    from loguru import Logger

# Context variable for request ID - thread-safe storage for per-request data
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

# Valid log levels - imported from config module to avoid circular imports
# Re-exported here for backward compatibility
from hsync_server.config import VALID_LOG_LEVELS

# Valid log formats
VALID_LOG_FORMATS: Final[tuple[str, ...]] = ("text", "json")


def get_request_id() -> str | None:
    """Get the current request ID from context."""
    return request_id_var.get()


def set_request_id(request_id: str | None) -> None:
    """Set the request ID in context for the current request."""
    request_id_var.set(request_id)


class InterceptHandler(BaseInterceptHandler):
    """
    Handler that intercepts standard logging and redirects to loguru.

    Binds the current request_id (from the contextvar) to every log record
    so that downstream loguru handlers can include it in their output.
    """

    def __init__(self) -> None:
        super().__init__(
            bind_extra=lambda: {"request_id": get_request_id() or "N/A"},
        )


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


def _format_record_colored(record: Any) -> str:
    """
    Custom format function for loguru with colors.

    Uses loguru's built-in color tags for proper terminal compatibility
    and automatic color stripping when output is not a TTY.

    Format: 2024-01-15T10:30:45.123Z - [12345] [req-abc123] INFO: message
    """
    request_id = record["extra"].get("request_id", "N/A")
    timestamp = record["time"].strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    level = record["level"].name
    message = record["message"]

    # Get level style tags
    level_start, level_end = LEVEL_STYLES.get(level, ("", ""))

    return (
        f"<dim>{timestamp}</dim> - [{record['process'].id}] [{request_id}] "
        f"{level_start}{level}{level_end}: {message}\n"
    )


def _format_record_plain(record: Any) -> str:
    """
    Custom format function for loguru without colors.

    Format: 2024-01-15T10:30:45.123Z - [12345] [req-abc123] INFO: message
    """
    request_id = record["extra"].get("request_id", "N/A")
    timestamp = record["time"].strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    return (
        f"{timestamp} - [{record['process'].id}] [{request_id}] "
        f"{record['level'].name}: {record['message']}\n"
    )


# Backward-compatible alias for the shared rotation config helper.
_get_rotation_config = get_rotation_config


def setup_logging(
    level: str = "info",
    log_format: str | None = None,
    no_color: bool = False,
    logfile: str | None = None,
) -> "Logger":
    """
    Configure and return the HSYNC server logger.

    Sets up logging with enterprise-grade features including request ID
    correlation, colored output, optional JSON structured output, and
    log rotation for file output.

    Log rotation is configured via environment variables:
    - HSYNC_LOG_FILE: Path to log file (alternative to logfile parameter)
    - HSYNC_LOG_ROTATION: When to rotate (default: "10 MB")
      Examples: "10 MB", "1 day", "00:00" (midnight), "1 week"
    - HSYNC_LOG_RETENTION: How long to keep old logs (default: "7 days")
      Examples: "7 days", "1 month", "5" (keep 5 files)
    - HSYNC_LOG_COMPRESSION: Compression format (default: "gz")
      Options: "gz", "bz2", "xz", "lzma", "tar", "tar.gz", "tar.bz2", "tar.xz", "zip"

    Args:
        level: Log level string (debug, info, warning, error, critical).
        log_format: Output format ("text" or "json"). If None, reads from
                   HSYNC_LOG_FORMAT environment variable.
        no_color: Disable colored output (default: False).
        logfile: Optional path to log file (enables rotation).

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

    # Get log file from parameter or environment variable
    log_file = logfile or os.environ.get("HSYNC_LOG_FILE")

    # Remove default loguru handler
    logger.remove()

    # Add console handler
    if log_format == "json":
        logger.add(
            sys.stdout,
            level=level.upper(),
            format="{message}",
            serialize=True,  # JSON output
        )
    else:
        # Colors are enabled only when:
        # 1. --no-color flag is not set
        # 2. Output is a TTY (terminal)
        use_colors = not no_color and sys.stdout.isatty()
        format_func = _format_record_colored if use_colors else _format_record_plain

        logger.add(
            sys.stdout,
            level=level.upper(),
            format=format_func,  # type: ignore[arg-type]
            colorize=use_colors,  # Enable loguru's color tag processing
        )

    # Add file handler with rotation if log file is specified
    if log_file:
        rotation_config = _get_rotation_config()
        logger.add(
            log_file,
            level=level.upper(),
            format=_format_record_plain,  # type: ignore[arg-type]
            colorize=False,
            encoding="utf-8",
            rotation=rotation_config["rotation"],
            retention=rotation_config["retention"],
            compression=rotation_config["compression"],
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
