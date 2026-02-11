# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
Health check and time synchronization endpoints for HSYNC Server.

This module provides:
- A simple health check endpoint for monitoring and load balancer health probes.
- A time endpoint for clock synchronization between clients and server.
"""

import time

from fastapi.responses import JSONResponse, PlainTextResponse
from loguru import logger as log

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


@public_router.get(
    "/api/v1/time",
    name="Clock synchronization endpoint",
)
async def get_time() -> JSONResponse:
    """
    Return the server's current timestamp for clock synchronization.

    Clients use this endpoint to calculate clock offset via round-trip
    measurement (NTP-like): the client records T1 before the request,
    the server returns T2, and the client records T3 after the response.
    Offset = T2 - (T1 + RTT/2), where RTT = T3 - T1.

    Returns:
        JSON with ``{"timestamp": <float>}`` (Unix epoch seconds).
    """
    return JSONResponse({"timestamp": time.time()})


log.info(f"Module {__name__} loaded")
