# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
Error handlers and response formatting for HSYNC Server.

This module provides:
- Global exception handlers for FastAPI
- Standardized error response format
- Mapping from custom exceptions to HTTP status codes
"""

from json import dumps as json_dumps
from typing import Any, Optional

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from loguru import logger as log
from pydantic import BaseModel

from .exceptions import (
    HSyncError,
    ValidationError,
    ResourceNotFoundError,
    AuthenticationError,
    FileOperationError,
    InvalidApiKeyError,
    MissingApiKeyError,
)


def _safe_json(obj: object) -> str:
    """Serialize object to JSON and escape braces for loguru."""
    return json_dumps(obj).replace("{", "{{").replace("}", "}}")


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


async def hsync_exception_handler(_request: Request, exc: HSyncError) -> JSONResponse:
    """
    Handle HSYNC custom exceptions.

    Converts HSyncError exceptions to standardized JSON responses
    with appropriate HTTP status codes.

    Args:
        _request: The incoming request (unused but required by FastAPI).
        exc: The HSyncError exception.

    Returns:
        JSONResponse with error details.
    """
    status_code = get_status_code_for_exception(exc)

    # Log based on severity
    if status_code >= 500:
        log.error(f"{exc.error_code}: {exc.message}", exc_info=True)
    elif status_code >= 400:
        log.warning(f"{exc.error_code}: {exc.message}")

    return JSONResponse(
        status_code=status_code,
        content=exc.to_dict(),
    )


async def validation_exception_handler(
    _request: Request, exc: RequestValidationError
) -> JSONResponse:
    """
    Handle Pydantic validation errors.

    Converts FastAPI/Pydantic validation errors to standardized format.

    Args:
        _request: The incoming request (unused but required by FastAPI).
        exc: The validation exception.

    Returns:
        JSONResponse with validation error details.
    """
    errors = exc.errors()
    log.warning(f"Validation error: {_safe_json(errors)}")

    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content={
            "error": "HSYNC-1000",
            "message": "Validation error",
            "details": {"validation_errors": errors},
        },
    )


async def global_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
    """
    Handle all unhandled exceptions.

    This is the last line of defense - catches any exception that
    wasn't handled by more specific handlers. Logs the full traceback
    but returns a generic error message to avoid leaking internal details.

    Args:
        _request: The incoming request (unused but required by FastAPI).
        exc: The unhandled exception.

    Returns:
        JSONResponse with generic error message.
    """
    log.error(f"Unhandled exception: {exc}", exc_info=True)

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
    app.add_exception_handler(HSyncError, hsync_exception_handler)  # type: ignore[arg-type]

    # Handle Pydantic validation errors
    app.add_exception_handler(RequestValidationError, validation_exception_handler)  # type: ignore[arg-type]

    # Handle all other unhandled exceptions (must be last)
    app.add_exception_handler(Exception, global_exception_handler)  # type: ignore[arg-type]

