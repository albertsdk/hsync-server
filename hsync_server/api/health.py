# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
Health check endpoint for HSYNC Server.

This module provides a simple health check endpoint for monitoring
and load balancer health probes. Returns an empty 200 OK response
to indicate the server is running.
"""

from fastapi.responses import PlainTextResponse
from loguru import logger as LOG

from hsync_server.routers import public_router


@public_router.get(
    "/health",
    name="Monitoring - Health check endpoint",
    response_class=PlainTextResponse,
)
async def root() -> str:
    """
    Health check endpoint.

    Returns an empty response with 200 OK status to indicate
    the server is healthy and accepting requests.

    Returns:
        Empty string (200 OK response).
    """
    return ""


LOG.info(f"Module {__name__} loaded")
