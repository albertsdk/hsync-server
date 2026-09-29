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
- Gzip compression of API responses that never touches transfer payloads
"""

import re
import uuid

from fastapi import Request
from loguru import logger as log
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.middleware.gzip import GZipMiddleware
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from .constants import (
    HEADER_CLIENT_NAME,
    HEADER_PROTOCOL_VERSION,
    HEADER_SERVER_VERSION,
    HSYNC_VERSION,
    PROTOCOL_VERSION_REST,
    is_valid_client_name,
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

    For unhandled exceptions (which bypass ``BaseHTTPMiddleware.dispatch``
    because of Starlette's ``ServerErrorMiddleware``), the companion
    ``register_exception_handlers`` function registers a global exception
    handler that still injects the ``X-Request-ID`` header.

    Usage:
        app.add_middleware(RequestIdMiddleware)
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # Get existing request ID from header or generate a new one
        request_id = request.headers.get("X-Request-ID")
        if not request_id:
            request_id = str(uuid.uuid4())

        # Store on request state so exception handlers can retrieve it
        # even after the context-local is cleared by this finally block.
        request.state.request_id = request_id

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
    4. Validates the X-Client-Name slug when present: it must identify a
       product ("hsync-client", "hsync-android"), never an internal module
       or file, so values like "main_logic.py" are rejected with 400

    Missing headers are accepted so non-client consumers (health checks, the
    interactive docs, browsers) keep working; only an explicit mismatch or
    malformed value is rejected.

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
        # Advertised on every response (accepted and rejected alike).
        version_headers = {
            HEADER_SERVER_VERSION: HSYNC_VERSION,
            HEADER_PROTOCOL_VERSION: PROTOCOL_VERSION_REST,
        }

        # Validate the client-name slug first: a malformed product identifier
        # is rejected outright (400), regardless of its protocol version. The
        # validation rule is the SSOT in hsync_common.constants.
        client_name = request.headers.get(HEADER_CLIENT_NAME, "")
        if client_name and not is_valid_client_name(client_name):
            log.warning(f"Rejecting client with malformed name {client_name!r}")
            return JSONResponse(
                status_code=400,
                content={
                    "error": "Malformed client name",
                    "client_name": client_name,
                },
                headers=version_headers,
            )

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
                headers=version_headers,
            )

        # Process the request
        response = await call_next(request)

        # Always advertise the server's app and protocol versions.
        response.headers.update(version_headers)

        return response


# Request paths that stream hsync payload *bytes*: the full-file download and
# the delta literal-payload download. Those bodies are not API metadata: their
# coding is part of the wire protocol (plain ``identity`` or
# ``Content-Encoding: zstd``, see
# hsync-common/docs/internal/adr/0010-zstandard-compression.md), and the client
# aborts a download on any other coding (hsync_client/transfer.py). A generic
# HTTP compressor must therefore never re-encode them.
_TRANSFER_PAYLOAD_PATH = re.compile(r"^/api/v1/buckets/[^/]+/files/(?:download|delta/data/[^/]+)$")


class TransferAwareGZipMiddleware:
    """
    Compress API responses with gzip but never hsync transfer payloads.

    Thin wrapper around Starlette's ``GZipMiddleware``: responses to the
    transfer endpoints (``…/files/download`` and
    ``…/files/delta/data/{token}``) bypass the compressor and reach the client
    byte-identical, while every other response keeps the size-based gzip
    behaviour.

    The exclusion is not an optimization but a contract requirement. The
    download endpoints are the only surface where the response coding belongs
    to the sync protocol, and the client enforces it strictly: it accepts
    ``identity`` or ``zstd`` and fails the transfer on anything else. Gzip would
    also replace ``Content-Length`` with chunked transfer, invalidating the
    Range/resume byte offsets the client relies on.

    Note that ``GZipMiddleware`` compresses the *first body chunk* of a streamed
    response regardless of its size, so a small file download would still be
    advertised as ``Content-Encoding: gzip``; path exclusion is the only
    reliable boundary (the response content type is guessed from the file name).

    Usage:
        app.add_middleware(TransferAwareGZipMiddleware, minimum_size=1000)
    """

    def __init__(self, app: ASGIApp, minimum_size: int = 1000) -> None:
        self._app = app
        self._gzip = GZipMiddleware(app, minimum_size=minimum_size)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and _TRANSFER_PAYLOAD_PATH.match(str(scope.get("path", ""))):
            await self._app(scope, receive, send)
            return
        await self._gzip(scope, receive, send)
