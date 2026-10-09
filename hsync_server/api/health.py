# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""
Health check and time synchronization endpoints for HSYNC Server.

This module provides:
- A simple health check endpoint for monitoring and load balancer health probes.
- A time endpoint for clock synchronization between clients and server.
"""

import time

from fastapi.responses import JSONResponse, PlainTextResponse
from hsync_common.protocol.rest import REST_HEALTH_PATH, REST_TIME_PATH
from loguru import logger as log

from hsync_server.routers import public_router

# Hard cap on the /health probe payload: bounds the amplification surface of
# an unauthenticated endpoint that can return an arbitrary number of bytes.
# Kept at the client autotune default (256 KB) — the autotune probe is
# unauthenticated by design, so the cap is the mitigation (finding F-01).
HEALTH_MAX_PROBE_BYTES = 256 * 1024  # 256 KB


@public_router.get(
    REST_HEALTH_PATH,
    name="Monitoring - Health check endpoint",
    response_class=PlainTextResponse,
)
async def root(size: int = 0) -> str:
    """
    Health check endpoint.

    Returns an empty response with 200 OK status to indicate
    the server is healthy and accepting requests.

    When ``size`` is supplied, returns that many bytes (bounded to
    ``HEALTH_MAX_PROBE_BYTES``) so a client can measure throughput for
    auto-tuning. The payload is a deterministic NUL-byte string — never
    reflected request input.

    Returns:
        Empty string (200 OK response), or ``size`` NUL bytes.
    """
    if size:
        size = max(0, min(size, HEALTH_MAX_PROBE_BYTES))
        return "\0" * size
    return ""


@public_router.get(
    REST_TIME_PATH,
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
