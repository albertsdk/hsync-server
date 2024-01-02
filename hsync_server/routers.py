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
Router configuration module for HSYNC Server.

This module defines the API routers and authentication middleware.
It provides two router types:
- auth_router: Requires API key authentication via Bearer token
- public_router: No authentication required (health checks, metrics)
"""

from .config import HSYNC_API_KEY
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    status,
)
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from logging import getLogger
from typing import Annotated


# Logger instance for this module
LOG = getLogger("main")


def _handle_error(message: str, status_code: int) -> None:
    """
    Helper function for error handling and logging.

    Logs the error message and raises an HTTPException with the
    specified status code.

    Args:
        message: Error message to log and return to client.
        status_code: HTTP status code for the response.

    Raises:
        HTTPException: Always raised with the provided status code and message.
    """
    LOG.error(message)
    raise HTTPException(status_code=status_code, detail=message)


def mask_api_key(api_key: str) -> str:
    """
    Mask an API key for safe logging.

    Shows only the first 3 and last 3 characters, replacing the
    middle portion with asterisks.

    Args:
        api_key: The API key to mask.

    Returns:
        Masked API key string (e.g., "abc****xyz").
    """
    return api_key[:3] + "*" * (len(api_key) - 10) + api_key[-3:]


def verify_api_key(
    token: Annotated[HTTPAuthorizationCredentials, Depends(HTTPBearer())],
) -> None:
    """
    Verify the API key from the Authorization header.

    This function is used as a FastAPI dependency to protect
    authenticated endpoints. It extracts the Bearer token from
    the Authorization header and validates it against the
    configured HSYNC_API_KEY.

    Args:
        token: HTTP Authorization credentials containing the Bearer token.

    Raises:
        HTTPException: 403 if no token provided, 401 if token is invalid.
    """
    api_key = token.credentials
    if not api_key:
        return _handle_error("Undefined signature", status.HTTP_403_FORBIDDEN)

    if api_key != HSYNC_API_KEY:
        _handle_error(
            f"Invalid signature: {mask_api_key(api_key)}",
            status.HTTP_401_UNAUTHORIZED,
        )


# Authenticated router - all endpoints require valid API key
auth_router = APIRouter(
    tags=["Authenticated Router"],
    dependencies=[
        Depends(verify_api_key),
    ],
)

# Public router - no authentication required
public_router = APIRouter(tags=["Public Router"])
