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
Constants for HSYNC Server.

This module contains all magic numbers and constant values used
throughout the application, making them easy to find and modify.
"""

# =============================================================================
# Application Info
# =============================================================================

# Application version (should match hsync-client version for compatibility)
HSYNC_VERSION = "1.0.0"


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


# =============================================================================
# HTTP Headers
# =============================================================================

# Custom headers used in responses
HEADER_MD5 = "X-MD5"
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

