# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
Literal-payload download endpoint for the delta transfer (download delta).

After POST ``/delta/signatures`` accepts a delta, the client fetches the
parked literal payload here. The token is single-use, short-lived and bound
to the ``(bucket, path)`` it was issued for; authentication is the standard
API-key dependency of ``auth_router`` (the token alone is not a credential).
The temp file is unlinked after the response has been streamed.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Annotated

from fastapi import BackgroundTasks, Depends, Request
from fastapi.responses import FileResponse, JSONResponse

from hsync_server.api.authz import check_bucket_access
from hsync_server.api.files.delta.tokens import delta_token_store
from hsync_server.auth import AuthContext, verify_api_key
from hsync_server.compression import CONTENT_ENCODING_ZSTD
from hsync_server.config import get_delta_enabled
from hsync_server.constants import CONTENT_TYPE_OCTET_STREAM
from hsync_server.rate_limiter import RATE_LIMIT_DELTA, limiter
from hsync_server.routers import auth_router


def _delta_disabled() -> JSONResponse:
    """404 response when the feature is off."""
    return JSONResponse(status_code=404, content={"detail": "delta_disabled"})


@auth_router.get("/api/v1/buckets/{bucket}/files/delta/data/{token}")
@limiter.limit(RATE_LIMIT_DELTA)
async def get_delta_data(
    request: Request,
    bucket: str,
    token: str,
    ctx: Annotated[AuthContext, Depends(verify_api_key)],
):
    """Stream the parked literal payload for a valid download token."""
    check_bucket_access(bucket, ctx)
    start_time = time.time()

    if not get_delta_enabled():
        return _delta_disabled()

    entry = delta_token_store.lookup(token)
    if entry is None:
        return JSONResponse(status_code=404, content={"detail": "token_not_found"})
    if delta_token_store.is_expired(entry):
        delta_token_store.discard(token)
        return JSONResponse(status_code=410, content={"detail": "token_expired"})
    # The token is bound to the bucket it was issued for: a token minted for
    # bucket A must never serve payloads through bucket B's URL.
    if entry.bucket != bucket:
        return JSONResponse(status_code=404, content={"detail": "token_not_found"})

    # Single-use: the entry leaves the registry before streaming starts.
    delta_token_store.consume(token)
    payload_file = entry.payload_file
    if not Path(payload_file).is_file():
        return JSONResponse(status_code=410, content={"detail": "token_expired"})

    background = BackgroundTasks()
    background.add_task(delta_token_store._delete_payload, entry)
    headers = {
        "Content-Disposition": 'attachment; filename="delta-data.bin"',
    }
    if entry.compressed:
        headers["Content-Encoding"] = CONTENT_ENCODING_ZSTD

    # Record metrics for successful data download
    from hsync_server.metrics import record_delta_transfer, record_transfer_duration

    record_delta_transfer("download", "success")
    record_transfer_duration("download", "data", time.time() - start_time)

    return FileResponse(
        payload_file,
        media_type=CONTENT_TYPE_OCTET_STREAM,
        headers=headers,
        background=background,
    )
