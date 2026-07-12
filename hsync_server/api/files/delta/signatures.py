# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
Delta signature exchange endpoints (upload-delta GET, download-delta POST).

GET ``/delta/signatures`` — the client, about to *upload*, asks for the
signatures of the server's base file; it then computes the delta locally.

POST ``/delta/signatures`` — the client, about to *download*, sends the
signatures of its local base file; the server matches them against its newer
file and, when worthwhile, returns the delta instructions plus a token URL
for the literal payload.

Both endpoints are thin HTTP shells over ``hsync_common.delta``: wire shapes,
status codes and fallbacks are identical to the stdlib LAN server.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from typing import Annotated

from aiofiles.ospath import isfile
from fastapi import Depends, Request
from fastapi.responses import JSONResponse
from hsync_common.constants import (
    DELTA_BLOCK_SIZE_LARGE,
    DELTA_BLOCK_SIZE_MEDIUM,
    DELTA_BLOCK_SIZE_SMALL,
    DELTA_BLOCK_SIZE_XLARGE,
    DELTA_MAX_LITERAL_SIZE,
)
from hsync_common.delta import (
    changed_block_ratio,
    compute_signatures,
    find_matches_file,
    is_delta_worthwhile,
    optimal_block_size,
)
from hsync_common.models.delta import (
    BlockSignature,
    BlockSignatureDict,
    sanitize_signature_list,
)
from loguru import logger as log
from pydantic import BaseModel, Field

from hsync_server.api.authz import check_bucket_access
from hsync_server.api.files.delta._util import delta_disabled_response
from hsync_server.api.files.delta.tokens import delta_token_store
from hsync_server.api.utils import is_valid_bucket, resolve_within_bucket
from hsync_server.auth import AuthContext, verify_api_key
from hsync_server.compression import _ZSTD_COMPRESSOR, should_compress
from hsync_server.config import get_compression_enabled, get_delta_enabled
from hsync_server.exceptions import (
    FileNotFoundError as HSyncFileNotFoundError,
)
from hsync_server.exceptions import (
    InvalidBucketNameError,
    InvalidPathError,
    ValidationError,
)
from hsync_server.hashes import get_or_compute_sha256
from hsync_server.metrics import (
    record_bytes_saved,
    record_change_ratio,
    record_delta_transfer,
    record_transfer_duration,
)
from hsync_server.rate_limiter import RATE_LIMIT_DELTA, limiter
from hsync_server.routers import auth_router

# Block sizes a peer may legitimately pick (adaptive table in common).
_VALID_BLOCK_SIZES = frozenset(
    {
        DELTA_BLOCK_SIZE_SMALL,
        DELTA_BLOCK_SIZE_MEDIUM,
        DELTA_BLOCK_SIZE_LARGE,
        DELTA_BLOCK_SIZE_XLARGE,
    }
)

# Maximum size of POST /delta/signatures body (50 MB) — bounds JSON payload
# and signature list size to prevent DoS via oversized requests.
_MAX_SIGNATURES_BODY_SIZE = 50 * 1024 * 1024  # 50 MB

# TTL (seconds) of the signature cache entries — same order as token TTL.
_SIGNATURE_CACHE_TTL = 300
_SIGNATURE_CACHE_MAX = 64

# (abs_path, sha256) -> (created, block_size, signatures). Signature computation
# is the dominant delta cost, so reuse it while the base file is unchanged.
# Guarded by ``_signature_cache_lock`` because ``_cached_signatures`` runs on
# asyncio worker threads (``asyncio.to_thread``), so several requests can
# miss/compute/evict concurrently; an unguarded FIFO eviction could pop the
# same key twice (KeyError) or iterate a dict mutated mid-loop.
_signature_cache: dict[tuple[str, str], tuple[float, int, list[BlockSignatureDict]]] = {}
_signature_cache_lock = threading.Lock()


def _cached_signatures(
    abs_path: str, sha256sum: str, file_size: int
) -> tuple[int, list[BlockSignatureDict]]:
    """Compute (or reuse cached) block signatures for an unchanged base file."""
    key = (abs_path, sha256sum)
    now = time.time()
    with _signature_cache_lock:
        hit = _signature_cache.get(key)
        if hit is not None and hit[0] >= now - _SIGNATURE_CACHE_TTL:
            return hit[1], hit[2]
    # Signature computation is expensive: run it without the lock held so a
    # concurrent request for another file is not blocked by this one.
    block_size = optimal_block_size(file_size)
    sigs = [s.to_dict() for s in compute_signatures(abs_path, block_size)]
    with _signature_cache_lock:
        # Another worker may have computed the same key meanwhile: prefer it
        # and drop ours, keeping one entry per key.
        current = _signature_cache.get(key)
        if current is not None and current[0] >= now - _SIGNATURE_CACHE_TTL:
            return current[1], current[2]
        if len(_signature_cache) >= _SIGNATURE_CACHE_MAX and _signature_cache:
            # Drop the oldest entry (dicts preserve insertion order).
            _signature_cache.pop(next(iter(_signature_cache)))
        _signature_cache[key] = (now, block_size, sigs)
    return block_size, sigs


class DeltaSignaturesRequest(BaseModel):
    """POST body: signatures of the client's local base file."""

    path: str = Field(min_length=1)
    block_size: int = Field(gt=0)
    signatures: list[BlockSignatureDict]


@auth_router.get("/api/v1/buckets/{bucket}/files/delta/signatures")
@limiter.limit(RATE_LIMIT_DELTA)
async def get_delta_signatures(
    request: Request,
    bucket: str,
    path: str,
    ctx: Annotated[AuthContext, Depends(verify_api_key)],
) -> JSONResponse:
    """Return block signatures of the server-side base file (upload delta).

    The client compares ``sha256`` with its expected ``base_hash`` first
    (early-out) and then scans its newer file against these signatures.
    """
    check_bucket_access(bucket, ctx)
    start_time = time.time()

    if not get_delta_enabled():
        return delta_disabled_response()
    if not is_valid_bucket(bucket):
        raise InvalidBucketNameError(details={"bucket": bucket})
    file = resolve_within_bucket(bucket, path)
    if file is None:
        raise InvalidPathError(details={"path": path})
    if not await isfile(file):
        raise HSyncFileNotFoundError(details={"bucket": bucket, "filename": path})

    file_size = Path(file).stat().st_size
    sha256sum = await get_or_compute_sha256(file)

    sig_start = time.time()
    block_size, sigs = await asyncio.to_thread(_cached_signatures, file, sha256sum, file_size)
    sig_duration = time.time() - sig_start

    # Record metrics for upload delta signatures request
    record_delta_transfer("upload", "success")
    record_transfer_duration("upload", "signatures", sig_duration)
    record_transfer_duration("upload", "total", time.time() - start_time)

    return JSONResponse(
        status_code=200,
        content={
            "block_size": block_size,
            "file_size": file_size,
            "sha256": sha256sum,
            "signatures": sigs,
        },
    )


@auth_router.post("/api/v1/buckets/{bucket}/files/delta/signatures")
@limiter.limit(RATE_LIMIT_DELTA)
async def post_delta_signatures(
    request: Request,
    bucket: str,
    ctx: Annotated[AuthContext, Depends(verify_api_key)],
) -> JSONResponse:
    """Match client base signatures against the server file (download delta).

    Returns ``{delta: true, ...}`` with instructions and a token URL for the
    literal payload, or ``{delta: false, reason}`` when a full transfer is
    cheaper (the client then falls back to a plain download).
    """
    check_bucket_access(bucket, ctx)
    start_time = time.time()

    # Read and validate body size before Pydantic parsing, regardless
    # of whether Content-Length is present or the client uses chunked
    # encoding — without this an attacker could bypass the 50 MB cap.
    body_bytes = await request.body()
    if len(body_bytes) > _MAX_SIGNATURES_BODY_SIZE:
        raise ValidationError(
            message="Request body too large",
            details={
                "max_size": _MAX_SIGNATURES_BODY_SIZE,
                "received_size": len(body_bytes),
            },
        )

    body = DeltaSignaturesRequest.model_validate_json(body_bytes)

    if not get_delta_enabled():
        return delta_disabled_response()
    if not is_valid_bucket(bucket):
        raise InvalidBucketNameError(details={"bucket": bucket})
    file = resolve_within_bucket(bucket, body.path)
    if file is None:
        raise InvalidPathError(details={"path": body.path})
    if not await isfile(file):
        raise HSyncFileNotFoundError(details={"bucket": bucket, "filename": body.path})

    if body.block_size not in _VALID_BLOCK_SIZES:
        raise ValidationError(
            message="Unsupported delta block size",
            details={"block_size": body.block_size},
        )
    clean_sigs = sanitize_signature_list([dict(s) for s in body.signatures])
    if clean_sigs is None:
        raise ValidationError(message="Invalid delta signatures payload")

    sigs = [BlockSignature(**s) for s in clean_sigs]

    new_size = Path(file).stat().st_size
    new_sha256 = await get_or_compute_sha256(file)

    match_start = time.time()

    def _match() -> tuple[list, bytes]:
        return find_matches_file(file, sigs, body.block_size)

    ops, literals = await asyncio.to_thread(_match)
    match_duration = time.time() - match_start

    # The base file size as seen by the client: covered span of its signatures.
    base_size = max((s.offset + s.length for s in sigs), default=0)
    ratio = changed_block_ratio(ops, base_size, body.block_size)

    # Record change ratio metric
    record_change_ratio("download", ratio)

    if not is_delta_worthwhile(ops, base_size, len(literals), body.block_size):
        reason = (
            "literal_payload_too_large"
            if len(literals) > DELTA_MAX_LITERAL_SIZE
            else "too_many_changed_blocks"
        )
        log.info(f"Delta not worthwhile for {body.path}: {reason} (ratio={ratio:.2f})")

        # Record rejected transfer
        record_delta_transfer("download", "rejected")
        record_transfer_duration("download", "total", time.time() - start_time)

        return JSONResponse(
            status_code=200,
            content={"delta": False, "reason": reason, "changed_percentage": round(ratio, 4)},
        )

    # Optionally compress the literal payload (served with Content-Encoding).
    # The shared compressor is reused: ZstdCompressor compress is stateless, so
    # a new instance (with its expensive table/context construction) is never
    # needed per request.
    compressed = False
    payload = literals
    if literals and get_compression_enabled() and should_compress(body.path, len(literals)):
        payload = await asyncio.to_thread(_ZSTD_COMPRESSOR.compress, literals)
        compressed = True

    token = await asyncio.to_thread(
        delta_token_store.issue,
        bucket=bucket,
        payload=payload,
        compressed=compressed,
    )

    # Record successful transfer and bytes saved
    record_delta_transfer("download", "success")
    record_transfer_duration("download", "match", match_duration)
    record_transfer_duration("download", "total", time.time() - start_time)
    # Bytes saved = original size - literal size (what we're actually sending)
    bytes_saved = max(0, new_size - len(literals))
    record_bytes_saved("download", bytes_saved)

    log.info(
        f"Delta download prepared for {body.path}: {len(ops)} ops, "
        f"{len(literals)} literal bytes (ratio={ratio:.2f})"
    )
    return JSONResponse(
        status_code=200,
        content={
            "delta": True,
            "block_size": body.block_size,
            "new_sha256": new_sha256,
            "instructions": {
                "new_size": new_size,
                "ops": [op.to_dict() for op in ops],
            },
            "data_url": f"/api/v1/buckets/{bucket}/files/delta/data/{token}",
            "data_size": len(literals),
            "compressed": compressed,
        },
    )
