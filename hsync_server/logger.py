"""
Logging configuration for HSYNC Server.

This module provides a standardized logging setup that matches the client's
format. The configuration is programmatic (no external YAML files needed).

Log format: 2024-01-15T10:30:45.123Z - [12345] INFO: message
"""

import logging
import sys

from typing import Final, Optional


# Log format matching client style (ISO 8601 timestamp with milliseconds)
LOG_FORMAT: Final[str] = "%(asctime)s.%(msecs)03d - [%(process)d] %(levelname)s: %(message)s"
LOG_DATE_FORMAT: Final[str] = "%Y-%m-%dT%H:%M:%SZ"

# Valid log levels (Python standard logging levels)
VALID_LOG_LEVELS: Final[tuple[str, ...]] = (
    "debug",
    "info",
    "warning",
    "error",
    "critical",
)

# Logger name for the main application
LOGGER_NAME: Final[str] = "main"


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


def setup_logging(level: str = "info") -> logging.Logger:
    """
    Configure and return the HSYNC server logger.

    Sets up logging with a format matching the client's output style.
    This function should be called once at application startup.

    Args:
        level: Log level string (debug, info, warning, error, critical).

    Returns:
        Configured logger instance.
    """
    # Get numeric log level
    log_level = get_log_level(level)

    # Get or create the main logger
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(log_level)

    # Clear any existing handlers to avoid duplicates
    logger.handlers.clear()

    # Create console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(log_level)

    # Create formatter
    formatter = logging.Formatter(fmt=LOG_FORMAT, datefmt=LOG_DATE_FORMAT)
    console_handler.setFormatter(formatter)

    # Add handler to logger
    logger.addHandler(console_handler)

    # Prevent propagation to root logger
    logger.propagate = False

    # Also configure uvicorn loggers to use the same format
    for uvicorn_logger_name in ("uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(uvicorn_logger_name)
        uvicorn_logger.handlers.clear()
        uvicorn_handler = logging.StreamHandler(sys.stdout)
        uvicorn_handler.setLevel(log_level)
        uvicorn_handler.setFormatter(formatter)
        uvicorn_logger.addHandler(uvicorn_handler)
        uvicorn_logger.propagate = False

    return logger

