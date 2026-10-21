# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

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

from collections.abc import Mapping
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from hsync_common.labels import colorize_labels
from hsync_common.logger import (
    BaseInterceptHandler,
    build_record_formatter,
    configure_handlers,
)
from hsync_common.runtime import ColorMode, color_output_enabled
from loguru import logger

from hsync_server.config import (
    VALID_LOG_LEVELS,
    get_log_file,
    get_tz,
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


# Format functions sharing the common factory; the server adds the per-request
# correlation id as an extra field and renders timestamps in the configured
# timezone (HSYNC_TZ, default UTC).
def _make_timestamp_formatter(tz: ZoneInfo):
    """Return a timestamp formatter rendering in the configured timezone."""

    def _format(record: Mapping[str, Any]) -> str:
        local = record["time"].astimezone(tz)
        return local.isoformat(timespec="milliseconds").replace("+00:00", "Z")

    return _format


def _build_formatters():
    """Build the colored/plain format functions for the configured timezone.

    Built at ``setup_logging`` time (not import time) so a late
    ``load_env_file`` that changes ``HSYNC_TZ`` is honoured.
    """
    timestamp_formatter = _make_timestamp_formatter(ZoneInfo(get_tz()))
    colored = build_record_formatter(
        colored=True,
        extra_fields=("request_id",),
        message_transform=_transform_colored,
        timestamp_formatter=timestamp_formatter,
    )
    plain = build_record_formatter(
        colored=False,
        extra_fields=("request_id",),
        timestamp_formatter=timestamp_formatter,
    )
    return colored, plain


def setup_logging(
    level: str = "info",
    log_format: str | None = None,
    color: ColorMode = ColorMode.AUTO,
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
        color: How to colorize stdout: auto (TTY only, honouring NO_COLOR),
            always or never.
        logfile: Optional path to log file (enables rotation).

    Returns:
        Configured loguru logger instance.
    """
    level_lower = level.lower()
    if level_lower not in VALID_LOG_LEVELS:
        valid = ", ".join(VALID_LOG_LEVELS)
        raise ValueError(f"Invalid log level: '{level}'. Must be one of: {valid}")

    # Determine log format
    if log_format is None:
        log_format = get_log_format()

    log_file = logfile or get_log_file()

    is_json = log_format == "json"
    # Colors follow --color (auto = TTY + NO_COLOR rules).
    use_colors = not is_json and color_output_enabled(color)
    format_colored, format_plain = _build_formatters()
    console_format = format_colored if use_colors else format_plain

    configure_handlers(
        level=level.upper(),
        console_format=console_format,
        colorize=use_colors,
        serialize_console=is_json,
        logfile=log_file,
        file_format=format_plain,
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
