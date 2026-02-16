# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
Router configuration module for HSYNC Server.

This module defines the API routers and authentication middleware.
It provides two router types:
- auth_router: Requires API key authentication via Bearer token
- public_router: No authentication required (health checks, metrics)
"""

import hmac
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from loguru import logger as log

from .config import HSYNC_API_KEY
from .exceptions import InvalidApiKeyError, MissingApiKeyError


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
        log.warning("Authentication attempt with missing API key")
        raise MissingApiKeyError()

    # Use constant-time comparison to prevent timing attacks
    if not hmac.compare_digest(api_key, HSYNC_API_KEY):
        log.warning(f"Authentication attempt with invalid API key: {mask_api_key(api_key)}")
        raise InvalidApiKeyError(details={"provided_key": mask_api_key(api_key)})


# Authenticated router - all endpoints require valid API key
auth_router = APIRouter(
    tags=["Authenticated Router"],
    dependencies=[
        Depends(verify_api_key),
    ],
)

# Public router - no authentication required
public_router = APIRouter(tags=["Public Router"])
