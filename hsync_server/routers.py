# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""
Router configuration module for HSYNC Server.

Defines two router types:
- ``auth_router``: requires account authentication via Bearer token.
- ``public_router``: no authentication (health, metrics, WebSocket).

The old ``verify_api_key``/``get_api_key`` functions have been replaced by
the account-based auth in ``hsync_server.auth``.
"""

from fastapi import APIRouter, Depends

from .auth import verify_api_key

# Authenticated router — all endpoints require a valid account token.
auth_router = APIRouter(
    tags=["Authenticated"],
    dependencies=[Depends(verify_api_key)],
)

# Public router — no authentication required.
public_router = APIRouter(tags=["Public"])
