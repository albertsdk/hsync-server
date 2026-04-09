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
- Automatic color detection (disabled for non-TTY or when NO_COLOR is set)
- File output support with log rotation

Log rotation configuration (via environment variables):
- HSYNC_LOG_FILE: Path to log file (enables file logging)
- HSYNC_LOG_ROTATION: When to rotate (e.g., "10 MB", "1 day", "00:00")
- HSYNC_LOG_RETENTION: How long to keep old logs (e.g., "7 days", "5")
- HSYNC_LOG_COMPRESSION: Compression format (default "xz"; also "gz", "zip", "bz2")

Log formats:
- Text: 2024-01-15T10:30:45.123Z - [12345] [req-abc123] INFO: message
- JSON: {"timestamp": "...", "level": "INFO", "pid": 12345, "request_id": "...", ...}
"""

from contextvars import ContextVar
from typing import TYPE_CHECKING

from hsync_common.labels import colorize_labels
from hsync_common.logger import (
    BaseInterceptHandler,
    build_record_formatter,
    configure_handlers,
)
from hsync_common.runtime import color_output_enabled
from loguru import logger

from hsync_server.config import (
    VALID_LOG_LEVELS,
    get_log_file,
)
from hsync_server.config import (
    get_log_format as get_config_log_format,
)

if TYPE_CHECKING:
    from loguru import Logger

# Context variable for request ID - thread-safe storage for per-request data
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)


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
    return get_config_log_format()


def _transform_colored(record, message: str) -> str:
    """Colorize action labels in the message."""
    return colorize_labels(message, use_colors=True)


def _transform_plain(record, message: str) -> str:
    """Return the message unchanged (no label colorizing)."""
    return message


# Format functions sharing the common factory; the server adds the per-request
# correlation id as an extra field.
_format_record_colored = build_record_formatter(
    colored=True, extra_fields=("request_id",), message_transform=_transform_colored
)
_format_record_plain = build_record_formatter(
    colored=False, extra_fields=("request_id",), message_transform=_transform_plain
)


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
    - HSYNC_LOG_COMPRESSION: Compression format (default: "xz")
      Options: "xz", "gz", "bz2", "lzma", "tar", "tar.gz", "tar.bz2", "tar.xz", "zip"

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

    # Get log file from parameter or environment-backed Settings
    log_file = logfile or get_log_file()

    is_json = log_format == "json"
    # Colors are enabled only when --no-color is not set, the NO_COLOR env is
    # unset, and stdout is a TTY.
    use_colors = not is_json and color_output_enabled(no_color=no_color)
    console_format = _format_record_colored if use_colors else _format_record_plain

    configure_handlers(
        level=level.upper(),
        console_format=console_format,
        colorize=use_colors,
        serialize_console=is_json,
        logfile=log_file,
        file_format=_format_record_plain,
        intercept_factory=InterceptHandler,
        intercept_logger_names=(
            "uvicorn",
            "uvicorn.error",
            "uvicorn.access",
            "hsync",
            "hsync_server",
        ),
    )

    return logger
