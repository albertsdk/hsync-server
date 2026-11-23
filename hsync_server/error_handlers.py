# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

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
from starlette.exceptions import HTTPException as StarletteHTTPException

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

    The field-level ``errors()`` detail stays in the log: echoing it back to an
    unauthenticated caller spells out the exact request schema, which is the
    kind of information disclosure this endpoint deliberately avoids.

    Args:
        _request: The incoming request (unused but required by FastAPI).
        exc: The validation exception.

    Returns:
        JSONResponse with the generic validation envelope.
    """
    errors = exc.errors()
    log.warning(f"Validation error: {safe_json_for_log(errors)}")

    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content={
            "error": "HSYNC-1000",
            "message": "Validation error",
        },
    )


# The only responses whose body the framework produces. Left alone they return
# ``{"detail": "Not Found"}``, a shape that identifies the stack to a probe.
_GENERIC_HTTP_ERRORS: dict[int, str] = {
    status.HTTP_404_NOT_FOUND: "Not Found",
    status.HTTP_405_METHOD_NOT_ALLOWED: "Method Not Allowed",
}


async def http_exception_handler(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """Answer framework-raised HTTP errors without a framework-shaped body.

    404 and 405 get a generic ``{"error": ...}`` envelope; the ``Allow`` header
    is preserved because it is part of the HTTP contract for 405. Every other
    status keeps FastAPI's default ``{"detail": ...}`` body and headers
    verbatim: those are deliberate API answers (401 with ``WWW-Authenticate``,
    429 from the rate limiter) that clients already consume.

    Args:
        _request: The incoming request (unused but required by FastAPI).
        exc: The HTTP exception raised by the router or a dependency.

    Returns:
        JSONResponse with a generic envelope, or the default body.
    """
    generic_message = _GENERIC_HTTP_ERRORS.get(exc.status_code)
    content = {"error": generic_message} if generic_message else {"detail": exc.detail}
    return JSONResponse(
        content=content,
        status_code=exc.status_code,
        headers=exc.headers,
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
    # Starlette-raised errors (404/405 from the router, HTTPException from a
    # dependency) first: this is what keeps framework-shaped bodies off the wire.
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)  # type: ignore[arg-type]

    app.add_exception_handler(HSyncError, hsync_exception_handler)  # type: ignore[arg-type]

    app.add_exception_handler(RequestValidationError, validation_exception_handler)  # type: ignore[arg-type]

    # Handle all other unhandled exceptions (must be last)
    app.add_exception_handler(Exception, global_exception_handler)  # type: ignore[arg-type]
