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
Health check endpoint for HSYNC Server.

This module provides a simple health check endpoint for monitoring
and load balancer health probes. Returns an empty 200 OK response
to indicate the server is running.
"""

from hsync_server.routers import public_router
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
