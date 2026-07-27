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
import time
from pathlib import Path

import zstandard as zstd
from aiofiles.ospath import isfile
from fastapi import Request
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
from hsync_common.hashing import compute_sha256_async
from hsync_common.models.delta import (
    BlockSignature,
    BlockSignatureDict,
    sanitize_signature_list,
)
from loguru import logger as log
from pydantic import BaseModel, Field

from hsync_server.api.files.delta.tokens import delta_token_store
from hsync_server.api.utils import is_valid_bucket, resolve_within_bucket
from hsync_server.compression import should_compress
from hsync_server.config import get_compression_enabled, get_delta_enabled
from hsync_server.exceptions import (
    FileNotFoundError as HSyncFileNotFoundError,
)
from hsync_server.exceptions import (
    InvalidBucketNameError,
    InvalidPathError,
    ValidationError,
)
from hsync_server.hashes import get_sha256_hash
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

# TTL (seconds) of the signature cache entries — same order as token TTL.
_SIGNATURE_CACHE_TTL = 300
_SIGNATURE_CACHE_MAX = 64

# (abs_path, sha256) -> (created, block_size, signatures). Signature computation
# is the dominant delta cost, so reuse it while the base file is unchanged.
_signature_cache: dict[tuple[str, str], tuple[float, int, list[BlockSignatureDict]]] = {}


def _cached_signatures(
    abs_path: str, sha256sum: str, file_size: int
) -> tuple[int, list[BlockSignatureDict]]:
    """Compute (or reuse cached) block signatures for an unchanged base file."""
    key = (abs_path, sha256sum)
    now = time.time()
    hit = _signature_cache.get(key)
    if hit is not None and hit[0] >= now - _SIGNATURE_CACHE_TTL:
        return hit[1], hit[2]
    block_size = optimal_block_size(file_size)
    sigs = [s.to_dict() for s in compute_signatures(abs_path, block_size)]
    if len(_signature_cache) >= _SIGNATURE_CACHE_MAX:
        # Drop the oldest entry (dicts preserve insertion order).
        _signature_cache.pop(next(iter(_signature_cache)))
    _signature_cache[key] = (now, block_size, sigs)
    return block_size, sigs


class DeltaSignaturesRequest(BaseModel):
    """POST body: signatures of the client's local base file."""

    path: str = Field(min_length=1)
    block_size: int = Field(gt=0)
    signatures: list[BlockSignatureDict]


def _delta_disabled() -> JSONResponse:
    """404 response when the feature is off — same as a pre-delta peer."""
    return JSONResponse(status_code=404, content={"detail": "Not Found"})


@auth_router.get("/api/v1/buckets/{bucket}/files/delta/signatures")
@limiter.limit(RATE_LIMIT_DELTA)
async def get_delta_signatures(request: Request, bucket: str, path: str) -> JSONResponse:
    """Return block signatures of the server-side base file (upload delta).

    The client compares ``sha256`` with its expected ``base_hash`` first
    (early-out) and then scans its newer file against these signatures.
    """
    if not get_delta_enabled():
        return _delta_disabled()
    if not is_valid_bucket(bucket):
        raise InvalidBucketNameError(details={"bucket": bucket})
    file = resolve_within_bucket(bucket, path)
    if file is None:
        raise InvalidPathError(details={"path": path})
    if not await isfile(file):
        raise HSyncFileNotFoundError(details={"bucket": bucket, "filename": path})

    file_size = Path(file).stat().st_size
    sha256sum = get_sha256_hash(file) or await compute_sha256_async(file)
    block_size, sigs = await asyncio.to_thread(_cached_signatures, file, sha256sum, file_size)
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
    request: Request, bucket: str, body: DeltaSignaturesRequest
) -> JSONResponse:
    """Match client base signatures against the server file (download delta).

    Returns ``{delta: true, ...}`` with instructions and a token URL for the
    literal payload, or ``{delta: false, reason}`` when a full transfer is
    cheaper (the client then falls back to a plain download).
    """
    if not get_delta_enabled():
        return _delta_disabled()
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

    sigs = [
        BlockSignature(
            index=s["index"],
            offset=s["offset"],
            length=s["length"],
            weak=s["weak"],
            strong=s["strong"],
        )
        for s in clean_sigs
    ]

    new_size = Path(file).stat().st_size
    new_sha256 = get_sha256_hash(file) or await compute_sha256_async(file)

    def _match() -> tuple[list, bytes]:
        return find_matches_file(file, sigs, body.block_size)

    ops, literals = await asyncio.to_thread(_match)

    # The base file size as seen by the client: covered span of its signatures.
    base_size = max((s.offset + s.length for s in sigs), default=0)
    ratio = changed_block_ratio(ops, base_size, body.block_size)
    if not is_delta_worthwhile(ops, base_size, len(literals), body.block_size):
        reason = (
            "literal_payload_too_large"
            if len(literals) > DELTA_MAX_LITERAL_SIZE
            else "too_many_changed_blocks"
        )
        log.info(f"Delta not worthwhile for {body.path}: {reason} (ratio={ratio:.2f})")
        return JSONResponse(
            status_code=200,
            content={"delta": False, "reason": reason, "changed_percentage": round(ratio, 4)},
        )

    # Optionally compress the literal payload (served with Content-Encoding).
    compressed = False
    payload = literals
    if literals and get_compression_enabled() and should_compress(body.path, len(literals)):
        payload = await asyncio.to_thread(zstd.ZstdCompressor().compress, literals)
        compressed = True

    token = await asyncio.to_thread(
        delta_token_store.issue,
        bucket=bucket,
        path=body.path,
        payload=payload,
        compressed=compressed,
    )
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
