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
Middleware components for HSYNC Server.

This module provides middleware for request processing, including:
- Request ID generation and propagation for distributed tracing
- Version compatibility checking between client and server
"""

import logging
import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .constants import HSYNC_VERSION, HEADER_CLIENT_VERSION, HEADER_SERVER_VERSION
from .logger import get_request_id, set_request_id


LOG = logging.getLogger(__name__)


def check_version_compatibility(
    local_version: str, remote_version: str
) -> tuple[bool, str]:
    """
    Check if remote version is compatible with local version using Semver.

    Compatibility rules:
    - Different major version: incompatible (reject)
    - Different minor version: warning but allowed
    - Different patch version: compatible

    Args:
        local_version: Local (server) version string (e.g., "0.1.0").
        remote_version: Remote (client) version string (e.g., "0.1.5").

    Returns:
        Tuple of (is_compatible, reason) where:
        - is_compatible: True if versions are compatible
        - reason: "compatible", "minor_mismatch", "major_mismatch", or "parse_error"
    """
    try:
        local_parts = local_version.split(".")
        remote_parts = remote_version.split(".")

        # Pad with zeros if needed
        while len(local_parts) < 3:
            local_parts.append("0")
        while len(remote_parts) < 3:
            remote_parts.append("0")

        # Compare major version
        if local_parts[0] != remote_parts[0]:
            return (False, "major_mismatch")

        # Compare minor version
        if local_parts[1] != remote_parts[1]:
            return (True, "minor_mismatch")

        return (True, "compatible")
    except Exception:
        return (True, "parse_error")


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

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
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
    Middleware that handles version compatibility between client and server.

    This middleware:
    1. Reads X-Client-Version header from incoming requests
    2. Checks version compatibility using Semver rules
    3. Rejects requests from clients with incompatible major version
    4. Adds X-Server-Version header to all responses

    Usage:
        app.add_middleware(VersionMiddleware)
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        """
        Process the request, checking version compatibility.

        Args:
            request: The incoming HTTP request.
            call_next: The next middleware or route handler.

        Returns:
            The HTTP response with X-Server-Version header.
            Returns 426 Upgrade Required if client version is incompatible.
        """
        # Get client version from header
        client_version = request.headers.get(HEADER_CLIENT_VERSION, "")

        # Check version compatibility if client sent version
        if client_version:
            is_compatible, reason = check_version_compatibility(
                HSYNC_VERSION, client_version
            )

            if not is_compatible:
                LOG.warning(
                    f"Rejecting client with incompatible version {client_version} "
                    f"(server: {HSYNC_VERSION}, reason: {reason})"
                )
                return JSONResponse(
                    status_code=426,
                    content={
                        "error": "Client version incompatible",
                        "client_version": client_version,
                        "server_version": HSYNC_VERSION,
                        "reason": reason,
                    },
                    headers={HEADER_SERVER_VERSION: HSYNC_VERSION},
                )

            if reason == "minor_mismatch":
                LOG.warning(
                    f"Client version {client_version} has different minor version "
                    f"(server: {HSYNC_VERSION}). Sync may have issues."
                )

        # Process the request
        response = await call_next(request)

        # Always add server version to response headers
        response.headers[HEADER_SERVER_VERSION] = HSYNC_VERSION

        return response
