# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""
Constants for HSYNC Server.

This module contains all magic numbers and constant values used
throughout the application, making them easy to find and modify.
"""

# =============================================================================
# Application Info
# =============================================================================
# Re-export shared constants from hsync-common
from hsync_common.constants import (  # noqa: F401
    CONTENT_TYPE_OCTET_STREAM,
    HEADER_CLIENT_NAME,
    HEADER_CLIENT_VERSION,
    HEADER_PROTOCOL_VERSION,
    HEADER_SERVER_VERSION,
    HSYNC_VERSION,
    HSYNCKEEP_FILENAME,
    is_valid_client_name,
)
from hsync_common.ids import CID_ALPHABET, mint_cid
from hsync_common.labels import ActionLabel  # noqa: F401
from hsync_common.protocol.handshake import PROTOCOL_VERSION_REST  # noqa: F401

# =============================================================================
# File Defaults
# =============================================================================

# Default file permissions for uploaded files (octal)
DEFAULT_FILE_PERMISSIONS = "664"

# Transient backup-file suffix: an in-place upload first copies the existing
# file to ``<name>.backup`` so readers keep seeing the complete original.
BACKUP_SUFFIX = ".backup"


# =============================================================================
# Alphanumeric IDs
# =============================================================================

# Canonical alphanumeric id alphabet — same shape as the wire CID ([A-Za-z0-9]).
# The alphabet and the mint are the hsync-common SSOT (``models.event``) so the
# server account id and the client CID cannot diverge.
ACCOUNT_ID_ALPHABET = CID_ALPHABET
ACCOUNT_ID_LEN = 21


def mint_alnum_id(length: int = ACCOUNT_ID_LEN) -> str:
    """Mint a cryptographically random alphanumeric id of *length* characters.

    Delegates to the shared :func:`hsync_common.models.event.mint_cid` (unbiased
    ``secrets.choice`` over the canonical 62-character alphabet).
    """
    return mint_cid(length)


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
