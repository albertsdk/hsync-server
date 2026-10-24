# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""
Literal-payload download tokens for the delta transfer endpoints.

When a download-delta request (POST ``/delta/signatures``) is accepted, the
server parks the literal payload in a temp file and hands the client a
single-use, short-lived token bound to ``(bucket, path)``; the client then
fetches the payload via GET ``/delta/data/{token}``.

The token lifecycle (TTL, capacity caps, storage accounting, lazy expiry,
single-use) is the shared implementation in
:class:`hsync_common.delta.token_store.DeltaTokenStore`; this module binds it to
the server's payload directory, caps and metrics.
"""

from __future__ import annotations

from pathlib import Path

from hsync_common.delta import DeltaTokenEntry, DeltaTokenStore

from hsync_server.config import get_buckets_path, get_delta_token_ttl
from hsync_server.metrics import update_active_tokens, update_temp_storage

__all__ = [
    "DELTA_MAX_ACTIVE_TOKENS",
    "DELTA_MAX_TEMP_STORAGE_BYTES",
    "DeltaTokenEntry",
    "delta_tmp_dir",
    "delta_token_store",
]

# Hard cap on concurrently live tokens (DoS guard).
DELTA_MAX_ACTIVE_TOKENS = 1_000

# Hard cap on total temp file storage (10 GB) — prevents disk exhaustion from
# accumulated literal payloads when tokens are not consumed promptly.
DELTA_MAX_TEMP_STORAGE_BYTES = 10 * 1024 * 1024 * 1024  # 10 GB

# Subdirectory of the buckets root holding literal payloads. It sits *outside*
# every bucket, so it is never picked up by sync listings.
_DELTA_TMP_DIRNAME = ".delta-tokens"


def delta_tmp_dir() -> Path:
    """Return (creating if needed) the directory holding literal payloads."""
    tmp = Path(get_buckets_path()) / _DELTA_TMP_DIRNAME
    tmp.mkdir(parents=True, exist_ok=True)
    return tmp


def _report_store_size(tokens: int, storage_bytes: int) -> None:
    """Publish the registry size to the metrics gauges."""
    update_active_tokens(tokens)
    update_temp_storage(storage_bytes)


# Process-wide store shared by the endpoints.
delta_token_store = DeltaTokenStore(
    ttl=get_delta_token_ttl,
    max_tokens=DELTA_MAX_ACTIVE_TOKENS,
    max_storage_bytes=DELTA_MAX_TEMP_STORAGE_BYTES,
    tmp_dir=delta_tmp_dir,
    on_change=_report_store_size,
)
