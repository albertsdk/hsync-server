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

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from hsync_common.text import safe_json_for_log
from loguru import logger as log

from .exceptions import (
    AuthenticationError,
    ConflictError,
    ConflictLostError,
    FileOperationError,
    ForbiddenError,
    HSyncError,
    InsufficientStorageError,
    InvalidApiKeyError,
    MissingApiKeyError,
    ResourceNotFoundError,
    SupersededError,
    ValidationError,
)

# Mapping from exception types to HTTP status codes
EXCEPTION_STATUS_CODES: dict[type, int] = {
    ValidationError: status.HTTP_400_BAD_REQUEST,
    ResourceNotFoundError: status.HTTP_404_NOT_FOUND,
    InvalidApiKeyError: status.HTTP_401_UNAUTHORIZED,
    MissingApiKeyError: status.HTTP_403_FORBIDDEN,
    ForbiddenError: status.HTTP_403_FORBIDDEN,
    ConflictError: status.HTTP_409_CONFLICT,
    AuthenticationError: status.HTTP_401_UNAUTHORIZED,
    SupersededError: status.HTTP_409_CONFLICT,
    ConflictLostError: status.HTTP_409_CONFLICT,
    InsufficientStorageError: status.HTTP_507_INSUFFICIENT_STORAGE,
    FileOperationError: status.HTTP_500_INTERNAL_SERVER_ERROR,
    HSyncError: status.HTTP_500_INTERNAL_SERVER_ERROR,
}


def get_status_code_for_exception(exc: HSyncError) -> int:
    """Get the HTTP status code for *exc* by exact type, walking its MRO.

    Walking ``type(exc).__mro__`` makes the lookup order-independent: the most
    specific class with a mapping wins, so adding a new exception type can never
    silently change another type's status code (the previous ``isinstance`` scan
    depended on hand-maintained dict insertion order).

    Args:
        exc: The exception to get status code for.

    Returns:
        HTTP status code.
    """
    for exc_type in type(exc).__mro__:
        status_code = EXCEPTION_STATUS_CODES.get(exc_type)
        if status_code is not None:
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
    if status_code >= status.HTTP_500_INTERNAL_SERVER_ERROR:
        log.error(f"{exc.error_code}: {exc.message}", exc_info=True)
    elif status_code >= status.HTTP_400_BAD_REQUEST:
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
    log.warning(f"Validation error: {safe_json_for_log(errors)}")

    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
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

    The ``X-Request-ID`` header is injected here because unhandled
    exceptions bypass ``BaseHTTPMiddleware.dispatch`` (Starlette's
    ``ServerErrorMiddleware`` swallows them first), so the regular
    ``RequestIdMiddleware`` cannot add the header for these responses.
    Instead the request ID is retrieved from ``request.state.request_id``,
    which is set by ``RequestIdMiddleware`` before the exception occurs.

    Args:
        request: The incoming request (used to retrieve the request ID).
        exc: The unhandled exception.

    Returns:
        JSONResponse with generic error message and X-Request-ID header.
    """
    # Retrieve the request ID that RequestIdMiddleware stashed on request.state
    request_id = getattr(request.state, "request_id", None)

    log.error(f"Unhandled exception (request_id={request_id}): {exc}", exc_info=True)

    headers = {}
    if request_id:
        headers["X-Request-ID"] = request_id

    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "error": "HSYNC-5000",
            "message": "An unexpected error occurred",
        },
        headers=headers,
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
