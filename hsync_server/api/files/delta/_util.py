# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Shared helpers for the delta REST endpoints.

Single source for the disabled-feature response and the request preflight: the
canonical payload is ``{"detail": "delta_disabled"}`` (pinned by the black-box
test suite), and every delta endpoint authorizes the bucket and gates on the
feature the same way, so the signature-exchange, delta-data and delta-apply
endpoints can never drift apart.
"""

from __future__ import annotations

import time

from fastapi.responses import JSONResponse

from hsync_server.api.authz import check_bucket_access
from hsync_server.auth import AuthContext
from hsync_server.config import get_delta_enabled


def delta_disabled_response() -> JSONResponse:
    """404 response returned when the delta feature is disabled."""
    return JSONResponse(status_code=404, content={"detail": "delta_disabled"})


def token_not_found_response() -> JSONResponse:
    """404 response for an unknown, consumed or mis-bound download token."""
    return JSONResponse(status_code=404, content={"detail": "token_not_found"})


def token_expired_response() -> JSONResponse:
    """410 response for an expired download token or a vanished payload."""
    return JSONResponse(status_code=410, content={"detail": "token_expired"})


def delta_authorize(bucket: str, ctx: AuthContext) -> float:
    """Authorize the bucket and return the request start time."""
    check_bucket_access(bucket, ctx)
    return time.time()


def delta_preflight(bucket: str, ctx: AuthContext) -> tuple[float, JSONResponse | None]:
    """Authorize the bucket and gate on the delta feature.

    Returns ``(start_time, refusal)``: ``refusal`` is the "delta disabled"
    response the caller must return unchanged, or ``None`` when the request may
    proceed. An endpoint that must read and validate the request body before the
    gate uses ``delta_authorize`` and applies the gate itself.
    """
    start_time = delta_authorize(bucket, ctx)
    if not get_delta_enabled():
        return start_time, delta_disabled_response()
    return start_time, None
