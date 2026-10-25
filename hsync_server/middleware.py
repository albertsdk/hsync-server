# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

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
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .constants import (
    HEADER_CLIENT_NAME,
    HEADER_PROTOCOL_VERSION,
    HEADER_SERVER_VERSION,
    HSYNC_VERSION,
    PROTOCOL_VERSION_REST,
    is_valid_client_name,
)
from .logger import set_request_id

# Accepted incoming correlation IDs: a narrow charset prevents log markup
# injection and header abuse through a client-controlled value. Anything else
# is replaced by a freshly generated UUID.
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


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
        request_id = request.headers.get("X-Request-ID", "")
        if not _REQUEST_ID_RE.fullmatch(request_id):
            request_id = str(uuid.uuid4())

        # Store on request state so exception handlers can retrieve it
        # even after the context-local is cleared by this finally block.
        request.state.request_id = request_id

        set_request_id(request_id)

        try:
            # Process the request
            response = await call_next(request)

            response.headers["X-Request-ID"] = request_id

            return response
        finally:
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


class MaxRequestSizeMiddleware:
    """Reject HTTP request bodies larger than *max_size* before they are buffered.

    FastAPI reads and spools the entire request body — multipart file parts
    included — *before* it resolves the authentication dependency, so the
    per-endpoint size checks run too late to bound what a client can make the
    server store in memory or in a spool file. This middleware enforces the cap
    at the ASGI ``receive`` boundary: a declared ``Content-Length`` over the
    limit is answered immediately with 413, and a chunked body (no length) is
    aborted as soon as the running total crosses it.

    ``max_size <= 0`` disables the cap.

    Usage:
        app.add_middleware(MaxRequestSizeMiddleware, max_size=...)
    """

    def __init__(self, app: ASGIApp, max_size: int) -> None:
        self._app = app
        self._max_size = max_size

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self._max_size <= 0:
            await self._app(scope, receive, send)
            return

        for name, value in scope.get("headers", []):
            if name == b"content-length" and _declared_length_exceeds(value, self._max_size):
                await _send_request_too_large(send)
                return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self._max_size:
                    raise _RequestBodyTooLarge()
            return message

        response_started = False

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self._app(scope, limited_receive, tracking_send)
        except _RequestBodyTooLarge:
            if response_started:
                raise
            await _send_request_too_large(send)


class _RequestBodyTooLarge(BaseException):
    """Internal signal raised by :class:`MaxRequestSizeMiddleware`.

    Subclasses :class:`BaseException` deliberately: FastAPI converts any
    ``Exception`` raised while parsing the request body into a generic 400,
    which would swallow the signal before the middleware can answer 413.
    """


def _declared_length_exceeds(value: bytes, max_size: int) -> bool:
    """True when a ``Content-Length`` header value is a number above the cap."""
    try:
        return int(value) > max_size
    except ValueError:
        return False


async def _send_request_too_large(send: Send) -> None:
    """Emit a minimal 413 JSON response on the raw ASGI channel."""
    body = b'{"error": "HSYNC-1006", "message": "Request body too large"}'
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


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
