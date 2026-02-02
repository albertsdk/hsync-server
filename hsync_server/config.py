"""
Configuration module for HSYNC Server.

This module loads and exposes all configuration values from environment
variables with sensible defaults. Configuration includes server port,
API key for authentication, timezone, logging level, and paths for file storage.

Environment variables can be set via dev.env or prod.env files.
"""

import os

from logging import getLogger
from pathlib import Path


# Logger instance for this module
LOG = getLogger("main")

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

# Log level for the server (debug, info, warning, error, critical)
HSYNC_LOG_LEVEL: str = os.environ.get("HSYNC_LOG_LEVEL", "info").lower()

# Timezone in IANA format (e.g., Europe/Rome, America/New_York, UTC)
# See: https://en.wikipedia.org/wiki/List_of_tz_database_time_zones
HSYNC_TZ: str = os.environ.get("TZ", "UTC")

# Application root path - resolved absolute path to the project directory
HSYNC_PATH: str = str(Path(os.path.dirname(__file__) + "/..").resolve())

# Server API Key for authentication
# All authenticated endpoints require this key as a Bearer token
# Generate a secure key at: https://it-tools.tech/token-generator?symbols=true
HSYNC_API_KEY: str = os.environ.get("HSYNC_API_KEY", "")

# Local buckets storage path - directory where all bucket data is stored
HSYNC_BUCKETS: str = f"{HSYNC_PATH}/buckets"
