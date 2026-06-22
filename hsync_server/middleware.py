# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
Middleware components for HSYNC Server.

This module provides middleware for request processing, including:
- Request ID generation and propagation for distributed tracing
- Strict wire-protocol version enforcement between client and server
"""

import uuid

from loguru import logger as log
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .constants import (
    HEADER_PROTOCOL_VERSION,
    HEADER_SERVER_VERSION,
    HSYNC_VERSION,
    PROTOCOL_VERSION_REST,
)
from .logger import set_request_id


class RequestIdMiddleware(BaseHTTPMiddleware):
    """
    Middleware that generates and propagates request IDs for distributed tracing.

    This middleware:
    1. Checks for an existing X-Request-ID header in the incoming request
    2. Generates a new UUID if no X-Request-ID is present
    3. Sets the request ID in the context for logging
    4. Adds the X-Request-ID header to the response

    Usage:
        app.add_middleware(RequestIdMiddleware)
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        """
        Process the request, ensuring a request ID is set for tracing.

        Args:
            request: The incoming HTTP request.
            call_next: The next middleware or route handler.

        Returns:
            The HTTP response with X-Request-ID header.
        """
        # Get existing request ID from header or generate a new one
        request_id = request.headers.get("X-Request-ID")
        if not request_id:
            request_id = str(uuid.uuid4())

        # Set request ID in context for logging
        set_request_id(request_id)

        try:
            # Process the request
            response = await call_next(request)

            # Add request ID to response headers
            response.headers["X-Request-ID"] = request_id

            return response
        finally:
            # Clear the request ID from context
            set_request_id(None)


class VersionMiddleware(BaseHTTPMiddleware):
    """
    Middleware that enforces the wire-protocol version between client and server.

    This middleware:
    1. Reads the X-Protocol-Version header from incoming requests
    2. Hard-fails with 426 when it is present and differs from the server's
       protocol version (there is no legacy protocol to negotiate down to)
    3. Adds X-Server-Version and X-Protocol-Version to every response

    A missing header is accepted so non-client consumers (health checks, the
    interactive docs, browsers) keep working; only an explicit mismatch is
    rejected.

    Usage:
        app.add_middleware(VersionMiddleware)
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        """
        Process the request, enforcing the wire-protocol version.

        Args:
            request: The incoming HTTP request.
            call_next: The next middleware or route handler.

        Returns:
            The HTTP response with X-Server-Version and X-Protocol-Version
            headers, or 426 Upgrade Required on a protocol-version mismatch.
        """
        # Get the client's protocol version from the header (if any)
        client_protocol = request.headers.get(HEADER_PROTOCOL_VERSION, "")

        # Hard-fail on an explicit mismatch; capabilities never bridge versions.
        if client_protocol and client_protocol != PROTOCOL_VERSION_REST:
            log.warning(
                f"Rejecting client with incompatible protocol {client_protocol} "
                f"(server: {PROTOCOL_VERSION_REST})"
            )
            return JSONResponse(
                status_code=426,
                content={
                    "error": "Protocol version incompatible",
                    "client_protocol_version": client_protocol,
                    "server_protocol_version": PROTOCOL_VERSION_REST,
                },
                headers={
                    HEADER_SERVER_VERSION: HSYNC_VERSION,
                    HEADER_PROTOCOL_VERSION: PROTOCOL_VERSION_REST,
                },
            )

        # Process the request
        response = await call_next(request)

        # Always advertise the server's app and protocol versions.
        response.headers[HEADER_SERVER_VERSION] = HSYNC_VERSION
        response.headers[HEADER_PROTOCOL_VERSION] = PROTOCOL_VERSION_REST

        return response
