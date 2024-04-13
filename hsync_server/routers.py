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
Router configuration module for HSYNC Server.

This module defines the API routers and authentication middleware.
It provides two router types:
- auth_router: Requires API key authentication via Bearer token
- public_router: No authentication required (health checks, metrics)
"""

import hmac
import logging

from .config import HSYNC_API_KEY
from .exceptions import InvalidApiKeyError, MissingApiKeyError
from fastapi import APIRouter, Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from typing import Annotated


# Logger instance for this module (uses __name__ for module-level filtering)
LOG = logging.getLogger(__name__)


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
    if len(api_key) <= 6:
        return "*" * len(api_key)
    return api_key[:3] + "*" * (len(api_key) - 6) + api_key[-3:]


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
        MissingApiKeyError: If no API key is provided.
        InvalidApiKeyError: If the API key is invalid.
    """
    api_key = token.credentials
    if not api_key:
        LOG.warning("Authentication attempt with missing API key")
        raise MissingApiKeyError()

    # Use constant-time comparison to prevent timing attacks
    if not hmac.compare_digest(api_key, HSYNC_API_KEY):
        LOG.warning(f"Authentication attempt with invalid API key: {mask_api_key(api_key)}")
        raise InvalidApiKeyError(
            details={"provided_key": mask_api_key(api_key)}
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
