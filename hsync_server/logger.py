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
from typing import Final, Optional

from loguru import logger


# =============================================================================
# Log Rotation Configuration Defaults
# =============================================================================

# Default rotation: rotate when file reaches 10 MB
DEFAULT_LOG_ROTATION: Final[str] = "10 MB"

# Default retention: keep logs for 7 days
DEFAULT_LOG_RETENTION: Final[str] = "7 days"

# Default compression: gzip for space efficiency
DEFAULT_LOG_COMPRESSION: Final[str] = "gz"


# Context variable for request ID - thread-safe storage for per-request data
request_id_var: ContextVar[Optional[str]] = ContextVar("request_id", default=None)

# Valid log levels - imported from config module to avoid circular imports
# Re-exported here for backward compatibility
from hsync_server.config import VALID_LOG_LEVELS

# Valid log formats
VALID_LOG_FORMATS: Final[tuple[str, ...]] = ("text", "json")

# =============================================================================
# Color Configuration (Best Practices)
# =============================================================================
#
# Following industry-standard log coloring conventions:
# - Timestamps: dim/muted to reduce visual noise
# - DEBUG: gray/dim - low priority, verbose information
# - INFO: default/white - normal operational messages
# - SUCCESS: green - positive outcomes
# - WARNING: yellow - attention needed but not critical
# - ERROR: red - failures that need investigation
# - CRITICAL: bold red - severe issues requiring immediate action
# =============================================================================

# Loguru color tags for terminal output
# Using loguru's built-in color system for proper terminal compatibility
LEVEL_STYLES: dict[str, tuple[str, str]] = {
    "DEBUG": ("<dim>", "</dim>"),
    "INFO": ("", ""),
    "SUCCESS": ("<green>", "</green>"),
    "WARNING": ("<yellow>", "</yellow>"),
    "ERROR": ("<red>", "</red>"),
    "CRITICAL": ("<bold><red>", "</red></bold>"),
}


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


def _format_record_colored(record: dict) -> str:
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


def _format_record_plain(record: dict) -> str:
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


def _get_rotation_config() -> dict[str, Optional[str]]:
    """
    Get log rotation configuration from environment variables.

    Returns:
        Dictionary with rotation, retention, and compression settings.
    """
    return {
        "rotation": os.environ.get("HSYNC_LOG_ROTATION", DEFAULT_LOG_ROTATION),
        "retention": os.environ.get("HSYNC_LOG_RETENTION", DEFAULT_LOG_RETENTION),
        "compression": os.environ.get("HSYNC_LOG_COMPRESSION", DEFAULT_LOG_COMPRESSION),
    }


def setup_logging(
    level: str = "info",
    log_format: Optional[str] = None,
    no_color: bool = False,
    logfile: Optional[str] = None,
) -> "logger":  # type: ignore[valid-type]
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
            format=format_func,
            colorize=use_colors,  # Enable loguru's color tag processing
        )

    # Add file handler with rotation if log file is specified
    if log_file:
        rotation_config = _get_rotation_config()
        logger.add(
            log_file,
            level=level.upper(),
            format=_format_record_plain,
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
