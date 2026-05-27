# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
Constants for HSYNC Server.

This module contains all magic numbers and constant values used
throughout the application, making them easy to find and modify.
"""

# =============================================================================
# Application Info
# =============================================================================

# Re-export shared constants from hsync-common
from hsync_common.constants import (  # noqa: E402, F401
    HSYNCKEEP_FILENAME,
    VERSION,
)

# Application version (re-exported from hsync-common)
HSYNC_VERSION = VERSION


# =============================================================================
# API Configuration
# =============================================================================

# API version prefix
API_VERSION = "v1"
API_PREFIX = f"/api/{API_VERSION}"

# API endpoint paths (relative to API_PREFIX)
ENDPOINT_HEALTH = "/health"
ENDPOINT_BUCKETS = "/buckets"
ENDPOINT_FILES = "/files"
ENDPOINT_FILES_DOWNLOAD = "/files/download"
ENDPOINT_EVENTS = "/events"


# =============================================================================
# File Defaults
# =============================================================================

# Default file permissions for uploaded files
DEFAULT_FILE_PERMISSIONS = "-rw-rw-r--"

# Default directory permissions
DEFAULT_DIR_PERMISSIONS = "drwxrwxr-x"


# HSYNCKEEP_FILENAME is re-exported from hsync_common.constants above


# =============================================================================
# HTTP Headers
# =============================================================================

# Custom headers used in responses
HEADER_SHA256 = "X-SHA256"
HEADER_PERMISSIONS = "X-Permissions"
HEADER_CLIENT_NAME = "X-Client-Name"
HEADER_CLIENT_VERSION = "X-Client-Version"
HEADER_SERVER_VERSION = "X-Server-Version"


# =============================================================================
# Content Types
# =============================================================================

CONTENT_TYPE_JSON = "application/json"
CONTENT_TYPE_OCTET_STREAM = "application/octet-stream"


# =============================================================================
# WebSocket Messages
# =============================================================================

# Authentication message types
WS_MSG_TYPE_AUTH = "auth"
WS_MSG_STATUS_OK = "ok"
WS_MSG_STATUS_ERROR = "error"
