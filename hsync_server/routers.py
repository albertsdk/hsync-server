# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If received, this code can be found at the source:
# https://github.com/albertobasso/hsync

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
    tags=["Authenticated Router"],
    dependencies=[Depends(verify_api_key)],
)

# Public router — no authentication required.
public_router = APIRouter(tags=["Public Router"])
