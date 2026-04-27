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
    CONTENT_TYPE_OCTET_STREAM,
    HEADER_CLIENT_NAME,
    HEADER_CLIENT_VERSION,
    HEADER_PROTOCOL_VERSION,
    HEADER_SERVER_VERSION,
    HSYNC_VERSION,
    HSYNCKEEP_FILENAME,
    is_valid_client_name,
)
from hsync_common.labels import ActionLabel  # noqa: E402, F401
from hsync_common.protocol.handshake import (  # noqa: E402, F401
    PROTOCOL_VERSION_REST,
    PROTOCOL_VERSION_WS,
)

# =============================================================================
# File Defaults
# =============================================================================

# Default file permissions for uploaded files (octal)
DEFAULT_FILE_PERMISSIONS = "664"


# =============================================================================
# HTTP Headers
# =============================================================================

# Custom headers (values re-exported from hsync_common.constants, the SSOT):
# HEADER_CLIENT_NAME / HEADER_CLIENT_VERSION / HEADER_SERVER_VERSION.
# HEADER_PROTOCOL_VERSION carries the wire-protocol version (single source of
# truth in hsync_common.protocol). Unlike the app version, a mismatch here is
# a hard failure (no legacy support).


# =============================================================================
# Content Types
# =============================================================================

# CONTENT_TYPE_OCTET_STREAM is re-exported from hsync_common.constants above
# (SSOT shared with hsync-client and mirrored by hsync-android).


# =============================================================================
# WebSocket Messages
# =============================================================================

# Authentication message types
WS_MSG_TYPE_AUTH = "auth"
