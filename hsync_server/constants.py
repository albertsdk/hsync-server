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
from hsync_common.protocol.handshake import (  # noqa: E402, F401
    PROTOCOL_VERSION_REST,
    PROTOCOL_VERSION_WS,
)

# Application version (re-exported from hsync-common)
HSYNC_VERSION = VERSION


# =============================================================================
# File Defaults
# =============================================================================

# Default file permissions for uploaded files (octal)
DEFAULT_FILE_PERMISSIONS = "664"


# =============================================================================
# HTTP Headers
# =============================================================================

# Custom headers used in responses
HEADER_CLIENT_NAME = "X-Client-Name"
HEADER_CLIENT_VERSION = "X-Client-Version"
HEADER_SERVER_VERSION = "X-Server-Version"
# Wire-protocol version (single source of truth in hsync_common.protocol). Unlike
# the app version above, a mismatch here is a hard failure (no legacy support).
HEADER_PROTOCOL_VERSION = "X-Protocol-Version"


# =============================================================================
# Content Types
# =============================================================================

CONTENT_TYPE_OCTET_STREAM = "application/octet-stream"


# =============================================================================
# WebSocket Messages
# =============================================================================

# Authentication message types
WS_MSG_TYPE_AUTH = "auth"
