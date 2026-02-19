# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
Custom exceptions for HSYNC Server.

This module defines a hierarchy of exceptions for better error handling
and more informative error responses. Each exception includes an error
code for debugging and monitoring purposes.

Error Code Format: HSYNC-XXXX
- 1XXX: Validation errors
- 2XXX: Resource not found errors
- 3XXX: Authentication/Authorization errors
- 4XXX: File operation errors
- 5XXX: Internal server errors
"""

from typing import Any

from hsync_common.exceptions import HSyncError as _CommonHSyncError


class HSyncError(_CommonHSyncError):
    """
    Base exception for all HSYNC server errors.

    Inherits the common construction contract (``message``, ``error_code``,
    ``details``, ``to_dict()``) from ``hsync_common.exceptions.HSyncError``
    and keeps the server-specific default error code (``HSYNC-5000``).
    """

    error_code: str = "HSYNC-5000"
    default_message: str = "An unexpected error occurred"


# =============================================================================
# Validation Errors (1XXX)
# =============================================================================


class ValidationError(HSyncError):
    """Invalid input data provided."""

    error_code = "HSYNC-1000"
    default_message = "Validation error"


class InvalidBucketNameError(ValidationError):
    """Bucket name contains invalid characters."""

    error_code = "HSYNC-1001"
    default_message = "Invalid bucket name"


class InvalidPathError(ValidationError):
    """File path is invalid or contains path traversal."""

    error_code = "HSYNC-1002"
    default_message = "Invalid path"


class InvalidPermissionFormatError(ValidationError):
    """Permission format is invalid."""

    error_code = "HSYNC-1003"
    default_message = "Invalid permission format"


class ChecksumMismatchError(ValidationError):
    """SHA-256 checksum verification failed."""

    error_code = "HSYNC-1005"
    default_message = "Checksum mismatch"


class FileTooLargeError(ValidationError):
    """File exceeds maximum allowed size."""

    error_code = "HSYNC-1006"
    default_message = "File too large"


class InvalidFilenameError(ValidationError):
    """Filename contains invalid characters or is reserved."""

    error_code = "HSYNC-1007"
    default_message = "Invalid filename"


# =============================================================================
# Resource Not Found Errors (2XXX)
# =============================================================================


class ResourceNotFoundError(HSyncError):
    """Requested resource does not exist."""

    error_code = "HSYNC-2000"
    default_message = "Resource not found"


class BucketNotFoundError(ResourceNotFoundError):
    """Bucket does not exist."""

    error_code = "HSYNC-2001"
    default_message = "Bucket not found"


class FileNotFoundError(ResourceNotFoundError):
    """File does not exist."""

    error_code = "HSYNC-2002"
    default_message = "File not found"


# =============================================================================
# Authentication/Authorization Errors (3XXX)
# =============================================================================


class AuthenticationError(HSyncError):
    """Authentication failed."""

    error_code = "HSYNC-3000"
    default_message = "Authentication failed"


class InvalidApiKeyError(AuthenticationError):
    """API key is invalid or missing."""

    error_code = "HSYNC-3001"
    default_message = "Invalid API key"


class MissingApiKeyError(AuthenticationError):
    """API key was not provided."""

    error_code = "HSYNC-3002"
    default_message = "API key required"


class ForbiddenError(HSyncError):
    """The caller is authenticated but lacks permission."""

    error_code = "HSYNC-3003"
    default_message = "Forbidden"


class ConflictError(HSyncError):
    """The operation conflicts with existing state."""

    error_code = "HSYNC-3004"
    default_message = "Conflict"


class AccountNotFoundError(ResourceNotFoundError):
    """An account referenced by id or name does not exist."""

    error_code = "HSYNC-2003"
    default_message = "Account not found"


# =============================================================================
# File Operation Errors (4XXX)
# =============================================================================


class FileOperationError(HSyncError):
    """File operation failed."""

    error_code = "HSYNC-4000"
    default_message = "File operation failed"


class UploadError(FileOperationError):
    """File upload failed."""

    error_code = "HSYNC-4001"
    default_message = "Upload failed"


class DeleteError(FileOperationError):
    """File deletion failed."""

    error_code = "HSYNC-4003"
    default_message = "Delete failed"


class InsufficientStorageError(FileOperationError):
    """Insufficient storage space on server."""

    error_code = "HSYNC-4004"
    default_message = "Insufficient storage space"


class SupersededError(FileOperationError):
    """Upload is causally dominated by the stored version (stale/ancestor).

    No conflicted copy is created: the incoming content is fully contained in
    the winner's history, so nothing is lost. The client should download the
    current version instead.
    """

    error_code = "HSYNC-4005"
    default_message = "Upload superseded by a newer version"
    # Machine-readable outcome token of the causal upload contract (ADR-0022
    # §4.6), matching the token the LAN master returns so uploaders can branch
    # on the outcome regardless of the transport.
    token = "superseded"

    def to_dict(self) -> dict[str, Any]:
        """Serialize with the explicit contract token (ADR-0022 §4.6)."""
        payload = super().to_dict()
        payload["resolution"] = self.token
        return payload


class ConflictLostError(FileOperationError):
    """Upload lost a causal tie-break (true divergence).

    The incoming bytes are preserved on the receiver as a conflicted copy; the
    client should download the winning version.
    """

    error_code = "HSYNC-4006"
    default_message = "Upload lost a conflict tie-break; content preserved as conflicted copy"
    # Machine-readable outcome token (ADR-0022 §4.6), matching the LAN master.
    token = "conflict_lost"

    def to_dict(self) -> dict[str, Any]:
        """Serialize with the explicit contract token (ADR-0022 §4.6)."""
        payload = super().to_dict()
        payload["resolution"] = self.token
        return payload
