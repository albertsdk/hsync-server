# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If received, this code can be found at the source:
# https://github.com/albertosdk/hsync

"""
Bucket presence endpoint for HSYNC Server.

``GET /api/v1/buckets/{bucket}/presence`` returns the display-ready presence
counters for a bucket: ``connected`` clients (accounts with a live WebSocket)
over ``total`` clients (accounts with bucket membership). Admins are operators,
not share members, so they are excluded from both counters — the same source of
truth the WebSocket ``presence`` broadcast frame uses.

The counts come from the in-process connection registry, valid only under the
supported single-worker deployment (see
``hsync_server.api.connection_manager``).
"""

from typing import Annotated

from fastapi import Depends, Request
from fastapi.responses import JSONResponse

from hsync_server.api.authz import check_bucket_access
from hsync_server.api.connection_manager import bucket_manager
from hsync_server.auth import AuthContext, verify_api_key
from hsync_server.rate_limiter import RATE_LIMIT_DEFAULT, limiter
from hsync_server.routers import auth_router


@auth_router.get("/api/v1/buckets/{bucket}/presence")
@limiter.limit(RATE_LIMIT_DEFAULT)
async def get_presence(
    request: Request,  # Required by rate limiter
    bucket: str,
    ctx: Annotated[AuthContext, Depends(verify_api_key)],
) -> JSONResponse:
    """
    Get presence counters for the bucket share.

    Returns:
        JSON response with:
        - connected: accounts currently connected to this bucket (self included)
        - total: accounts with access to this bucket (self included)

    Raises:
        InvalidBucketNameError: If bucket name is invalid.
        BucketNotFoundError: If bucket is unknown or the caller has no access.
    """
    check_bucket_access(bucket, ctx)
    connected, total = bucket_manager.presence_counts(bucket)
    return JSONResponse(status_code=200, content={"connected": connected, "total": total})
