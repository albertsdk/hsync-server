"""
Configuration module for HSYNC Server.

This module loads and exposes all configuration values from environment
variables with sensible defaults. Configuration includes server port,
API key for authentication, and paths for file storage.
"""

import os

from logging import getLogger
from pathlib import Path


# Logger instance for this module
LOG = getLogger("main")

# Server port - the port on which the HSYNC server listens for connections
# Can be overridden via HSYNC_PORT environment variable
HSYNC_PORT: int = int(os.environ.get("HSYNC_PORT", 8765))

# Application root path - resolved absolute path to the project directory
HSYNC_PATH: str = str(Path(os.path.dirname(__file__) + "/..").resolve())

# Server API Key for authentication
# All authenticated endpoints require this key as a Bearer token
# Generate a secure key at: https://it-tools.tech/token-generator?symbols=true
HSYNC_API_KEY: str = os.environ.get("HSYNC_API_KEY", "")

# Local buckets storage path - directory where all bucket data is stored
HSYNC_BUCKETS: str = f"{HSYNC_PATH}/buckets"
