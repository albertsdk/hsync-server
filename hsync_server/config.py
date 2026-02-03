"""
Configuration module for HSYNC Server.

This module loads and exposes all configuration values from environment
variables with sensible defaults. Configuration includes server port,
API key for authentication, timezone, logging level, and paths for file storage.

Environment variables can be set via dev.env or prod.env files.
"""

import logging
import os

from pathlib import Path

from .logger import VALID_LOG_LEVELS


# Application root path - resolved absolute path to the project directory
HSYNC_PATH: str = str(Path(os.path.dirname(__file__) + "/..").resolve())

# Server port - the port on which the HSYNC server listens for connections
# Can be overridden via HSYNC_PORT environment variable
HSYNC_PORT: int = int(os.environ.get("HSYNC_PORT", 8765))

# Number of worker processes for uvicorn
# WARNING: Multi-worker mode (>1) is NOT recommended because:
# - WebSocket connections are NOT shared between workers (clients on different
#   workers cannot communicate)
# - MD5 hash cache (FILE_HASHES) is NOT shared between workers
# - Rate limiter state is NOT shared (limits apply per-worker, not globally)
# Use HSYNC_WORKERS=1 unless you implement Redis/shared state for these components
HSYNC_WORKERS: int = int(os.environ.get("HSYNC_WORKERS", 1))

# Timezone in IANA format (e.g., Europe/Rome, America/New_York, UTC)
# See: https://en.wikipedia.org/wiki/List_of_tz_database_time_zones
HSYNC_TZ: str = os.environ.get("HSYNC_TZ", "UTC")

# Server API Key for authentication
# All authenticated endpoints require this key as a Bearer token
# Generate a secure key at: https://it-tools.tech/token-generator?symbols=true
HSYNC_API_KEY: str = os.environ.get("HSYNC_API_KEY", "")

# Local buckets storage path - directory where all bucket data is stored
HSYNC_BUCKETS: str = f"{HSYNC_PATH}/buckets"


def _get_log_level() -> str:
    """
    Get and validate the log level from environment variable.

    Returns:
        Validated log level string (lowercase).

    Raises:
        ValueError: If the log level is not valid.
    """
    level = os.environ.get("HSYNC_LOG_LEVEL", "info").lower()
    if level not in VALID_LOG_LEVELS:
        valid = ", ".join(VALID_LOG_LEVELS)
        raise ValueError(
            f"Invalid HSYNC_LOG_LEVEL: '{level}'. Must be one of: {valid}"
        )
    return level


# Log level for the server (debug, info, warning, error, critical)
HSYNC_LOG_LEVEL: str = _get_log_level()

# Logger instance for this module
LOG = logging.getLogger("main")
