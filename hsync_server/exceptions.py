# HSYNC Server - HTTP-based file synchronization server
# Copyright (C) 2025 Alberto Basso
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

from typing import Any, Optional


class HSyncError(Exception):
    """
    Base exception for all HSYNC errors.

    Attributes:
        message: Human-readable error message.
        error_code: Unique error code for debugging (e.g., "HSYNC-1001").
        details: Optional additional error details.
    """

    error_code: str = "HSYNC-5000"
    default_message: str = "An unexpected error occurred"

    def __init__(
        self,
        message: Optional[str] = None,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        self.message = message or self.default_message
        self.details = details or {}
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        """Convert exception to dictionary for JSON response."""
        result: dict[str, Any] = {
            "error": self.error_code,
            "message": self.message,
        }
        if self.details:
            result["details"] = self.details
        return result


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


class InvalidSyncOrderError(ValidationError):
    """Sync order parameter is invalid."""

    error_code = "HSYNC-1004"
    default_message = "Invalid sync order"


class ChecksumMismatchError(ValidationError):
    """MD5 checksum verification failed."""

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


class DownloadError(FileOperationError):
    """File download failed."""

    error_code = "HSYNC-4002"
    default_message = "Download failed"


class DeleteError(FileOperationError):
    """File deletion failed."""

    error_code = "HSYNC-4003"
    default_message = "Delete failed"

