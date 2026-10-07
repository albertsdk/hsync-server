# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Shared ``JSONResponse`` factories for the API error payloads.

The admin and membership endpoints return the same machine-readable error
envelopes (``{"error": "HSYNC-XXXX", "message": ...}``) with only the message
varying; these factories are the single definition so the codes cannot drift.

Consumers: ``hsync_server.api.admin``, ``hsync_server.api.members``.
"""

from __future__ import annotations

from fastapi import status
from fastapi.responses import JSONResponse

__all__ = [
    "account_not_found_response",
    "conflict_response",
    "not_found_response",
]


def not_found_response(message: str) -> JSONResponse:
    """404 payload carrying the HSYNC-2000 resource-not-found code."""
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content={"error": "HSYNC-2000", "message": message},
    )


def account_not_found_response() -> JSONResponse:
    """404 payload for an unknown account id or name."""
    return not_found_response("Account not found.")


def conflict_response(message: str) -> JSONResponse:
    """409 payload carrying the HSYNC-3004 conflict code."""
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={"error": "HSYNC-3004", "message": message},
    )
