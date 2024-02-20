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

# =============================================================================
# CORS Configuration
# =============================================================================

# Enable CORS middleware (set to "true" to enable)
HSYNC_CORS_ENABLED: bool = os.environ.get("HSYNC_CORS_ENABLED", "false").lower() == "true"

# Allowed origins for CORS (comma-separated list)
# Example: "https://example.com,https://app.example.com"
# Use "*" to allow all origins (not recommended for production)
HSYNC_CORS_ORIGINS: list[str] = [
    origin.strip()
    for origin in os.environ.get("HSYNC_CORS_ORIGINS", "").split(",")
    if origin.strip()
]

# =============================================================================
# SSL/TLS Configuration
# =============================================================================

# Enable HTTPS/WSS (set to "true" to enable SSL)
HSYNC_SSL_ENABLED: bool = os.environ.get("HSYNC_SSL_ENABLED", "false").lower() == "true"

# Path to SSL certificate file (PEM format)
# If not set and SSL is enabled, certificates will be auto-generated
HSYNC_SSL_CERTFILE: str = os.environ.get("HSYNC_SSL_CERTFILE", "")

# Path to SSL private key file (PEM format)
# If not set and SSL is enabled, certificates will be auto-generated
HSYNC_SSL_KEYFILE: str = os.environ.get("HSYNC_SSL_KEYFILE", "")

# Directory for auto-generated certificates (default: {HSYNC_PATH}/certs)
# Certificates are generated automatically if HSYNC_SSL_CERTFILE/KEYFILE are not set
HSYNC_SSL_CERT_DIR: str = os.environ.get("HSYNC_SSL_CERT_DIR", f"{HSYNC_PATH}/certs")


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

# Logger instance for this module (uses __name__ for module-level filtering)
LOG = logging.getLogger(__name__)
