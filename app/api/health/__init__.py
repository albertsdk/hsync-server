"""
Health check endpoint for HSYNC Server.

This module provides a simple health check endpoint for monitoring
and load balancer health probes. Returns an empty 200 OK response
to indicate the server is running.
"""

from app.routers import public_router
from fastapi.responses import PlainTextResponse
from logging import getLogger


# Logger instance for this module
LOG = getLogger("main")


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
