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
Error handlers and response formatting for HSYNC Server.

This module provides:
- Global exception handlers for FastAPI
- Standardized error response format
- Mapping from custom exceptions to HTTP status codes
"""

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from logging import getLogger
from pydantic import BaseModel
from typing import Any, Optional

from .exceptions import (
    HSyncError,
    ValidationError,
    ResourceNotFoundError,
    AuthenticationError,
    FileOperationError,
    InvalidApiKeyError,
    MissingApiKeyError,
)


LOG = getLogger("main")


class ErrorResponse(BaseModel):
    """
    Standardized error response format.

    Attributes:
        error: Error code (e.g., "HSYNC-1001").
        message: Human-readable error message.
        details: Optional additional error details.
    """

    error: str
    message: str
    details: Optional[dict[str, Any]] = None


# Mapping from exception types to HTTP status codes
EXCEPTION_STATUS_CODES: dict[type, int] = {
    ValidationError: status.HTTP_400_BAD_REQUEST,
    ResourceNotFoundError: status.HTTP_404_NOT_FOUND,
    InvalidApiKeyError: status.HTTP_401_UNAUTHORIZED,
    MissingApiKeyError: status.HTTP_403_FORBIDDEN,
    AuthenticationError: status.HTTP_401_UNAUTHORIZED,
    FileOperationError: status.HTTP_500_INTERNAL_SERVER_ERROR,
    HSyncError: status.HTTP_500_INTERNAL_SERVER_ERROR,
}


def get_status_code_for_exception(exc: HSyncError) -> int:
    """
    Get the appropriate HTTP status code for an exception.

    Checks the exception type hierarchy to find the most specific
    status code mapping.

    Args:
        exc: The exception to get status code for.

    Returns:
        HTTP status code.
    """
    for exc_type, status_code in EXCEPTION_STATUS_CODES.items():
        if isinstance(exc, exc_type):
            return status_code
    return status.HTTP_500_INTERNAL_SERVER_ERROR


async def hsync_exception_handler(request: Request, exc: HSyncError) -> JSONResponse:
    """
    Handle HSYNC custom exceptions.

    Converts HSyncError exceptions to standardized JSON responses
    with appropriate HTTP status codes.

    Args:
        request: The incoming request.
        exc: The HSyncError exception.

    Returns:
        JSONResponse with error details.
    """
    status_code = get_status_code_for_exception(exc)

    # Log based on severity
    if status_code >= 500:
        LOG.error(f"{exc.error_code}: {exc.message}", exc_info=True)
    elif status_code >= 400:
        LOG.warning(f"{exc.error_code}: {exc.message}")

    return JSONResponse(
        status_code=status_code,
        content=exc.to_dict(),
    )


async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """
    Handle Pydantic validation errors.

    Converts FastAPI/Pydantic validation errors to standardized format.

    Args:
        request: The incoming request.
        exc: The validation exception.

    Returns:
        JSONResponse with validation error details.
    """
    errors = exc.errors()
    LOG.warning(f"Validation error: {errors}")

    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "error": "HSYNC-1000",
            "message": "Validation error",
            "details": {"validation_errors": errors},
        },
    )


async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """
    Handle all unhandled exceptions.

    This is the last line of defense - catches any exception that
    wasn't handled by more specific handlers. Logs the full traceback
    but returns a generic error message to avoid leaking internal details.

    Args:
        request: The incoming request.
        exc: The unhandled exception.

    Returns:
        JSONResponse with generic error message.
    """
    LOG.error(f"Unhandled exception: {exc}", exc_info=True)

    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "error": "HSYNC-5000",
            "message": "An unexpected error occurred",
        },
    )


def register_exception_handlers(app: FastAPI) -> None:
    """
    Register all exception handlers with the FastAPI application.

    Args:
        app: The FastAPI application instance.
    """
    # Handle custom HSYNC exceptions
    app.add_exception_handler(HSyncError, hsync_exception_handler)

    # Handle Pydantic validation errors
    app.add_exception_handler(RequestValidationError, validation_exception_handler)

    # Handle all other unhandled exceptions (must be last)
    app.add_exception_handler(Exception, global_exception_handler)

