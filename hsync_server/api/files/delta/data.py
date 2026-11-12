# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""
Literal-payload download endpoint for the delta transfer (download delta).

After POST ``/delta/signatures`` accepts a delta, the client fetches the
parked literal payload here. The token is single-use, short-lived and bound
to the bucket it was issued for; authentication is the standard
API-key dependency of ``auth_router`` (the token alone is not a credential).
The temp file is unlinked after the response has been streamed.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Annotated

from fastapi import BackgroundTasks, Depends, Request
from fastapi.responses import FileResponse
from hsync_common.protocol.rest import REST_DELTA_DATA_PATH_TEMPLATE

from hsync_server.api.files.delta._util import (
    delta_preflight,
    token_expired_response,
    token_not_found_response,
)
from hsync_server.api.files.delta.tokens import delta_token_store
from hsync_server.auth import AuthContext, verify_api_key
from hsync_server.compression import CONTENT_ENCODING_ZSTD
from hsync_server.constants import CONTENT_TYPE_OCTET_STREAM
from hsync_server.metrics import record_delta_transfer, record_transfer_duration
from hsync_server.rate_limiter import RATE_LIMIT_DELTA, limiter
from hsync_server.routers import auth_router


@auth_router.get(REST_DELTA_DATA_PATH_TEMPLATE)
@limiter.limit(RATE_LIMIT_DELTA)
async def get_delta_data(
    request: Request,
    bucket: str,
    token: str,
    ctx: Annotated[AuthContext, Depends(verify_api_key)],
):
    """Stream the parked literal payload for a valid download token."""
    start_time, refusal = delta_preflight(bucket, ctx)
    if refusal is not None:
        return refusal

    entry = delta_token_store.lookup(token)
    if entry is None:
        return token_not_found_response()
    if delta_token_store.is_expired(entry):
        delta_token_store.discard(token)
        return token_expired_response()
    # The token is a capability bound to the issuing account and bucket: a
    # token minted for bucket A / account X must never serve payloads through
    # bucket B or for account Y (a leaked URL is not transferable). Entries
    # without an account id come from producers that have no account identity.
    if entry.bucket != bucket or (entry.account_id and entry.account_id != ctx.id):
        return token_not_found_response()

    # Single-use: the entry leaves the registry before streaming starts.
    delta_token_store.consume(token)
    payload_file = entry.payload_file
    if not Path(payload_file).is_file():
        return token_expired_response()

    background = BackgroundTasks()
    background.add_task(delta_token_store._delete_payload, entry)
    headers = {
        "Content-Disposition": 'attachment; filename="delta-data.bin"',
    }
    if entry.compressed:
        headers["Content-Encoding"] = CONTENT_ENCODING_ZSTD

    # Record metrics for successful data download
    record_delta_transfer("download", "success")
    record_transfer_duration("download", "data", time.time() - start_time)

    return FileResponse(
        payload_file,
        media_type=CONTENT_TYPE_OCTET_STREAM,
        headers=headers,
        background=background,
    )
