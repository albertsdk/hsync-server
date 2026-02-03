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
Middleware components for HSYNC Server.

This module provides middleware for request processing, including:
- Request ID generation and propagation for distributed tracing
"""

import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from .logger import get_request_id, set_request_id


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

