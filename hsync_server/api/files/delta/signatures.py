# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

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
import uuid
from pathlib import Path
from typing import Annotated

from aiofiles.ospath import isfile
from fastapi import Depends, Request
from fastapi.responses import JSONResponse
from hsync_common.compression import compress_file_to_file
from hsync_common.constants import (
    DELTA_BLOCK_SIZE_LARGE,
    DELTA_BLOCK_SIZE_MEDIUM,
    DELTA_BLOCK_SIZE_SMALL,
    DELTA_BLOCK_SIZE_XLARGE,
    DELTA_MAX_LITERAL_SIZE,
)
from hsync_common.delta import (
    BoundedLiteralSink,
    changed_block_ratio,
    compute_signatures,
    find_matches_file_to_sink,
    is_delta_worthwhile,
    optimal_block_size,
)
from hsync_common.models.delta import (
    BlockSignature,
    BlockSignatureDict,
    delta_download_payload,
    delta_signatures_payload,
    sanitize_signature_list,
)
from hsync_common.protocol.rest import (
    REST_DELTA_SIGNATURES_SUFFIX,
    rest_bucket_template,
    rest_delta_data_path,
)
from loguru import logger as log
from pydantic import BaseModel, Field

from hsync_server.api.authz import check_bucket_access
from hsync_server.api.files.delta._util import delta_disabled_response
from hsync_server.api.files.delta.tokens import delta_tmp_dir, delta_token_store
from hsync_server.api.utils import read_request_body_capped, resolve_within_bucket
from hsync_server.auth import AuthContext, verify_api_key
from hsync_server.compression import COMPRESSION_LEVEL, should_compress
from hsync_server.config import get_compression_enabled, get_delta_enabled
from hsync_server.exceptions import (
    FileNotFoundError as HSyncFileNotFoundError,
)
from hsync_server.exceptions import (
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


@auth_router.get(rest_bucket_template(REST_DELTA_SIGNATURES_SUFFIX))
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
        content=delta_signatures_payload(
            block_size=block_size,
            file_size=file_size,
            sha256=sha256sum,
            signatures=sigs,
        ),
    )


# Cap-driven abort: once the literal payload would exceed DELTA_MAX_LITERAL_SIZE
# the delta is refused anyway, so ``BoundedLiteralSink`` (hsync-common) stops the
# match instead of growing the temp file (and burning CPU) on a transfer that is
# not worth doing.
@auth_router.post(rest_bucket_template(REST_DELTA_SIGNATURES_SUFFIX))
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
    # encoding — the body is streamed so it is never fully buffered first.
    body_bytes = await read_request_body_capped(request, _MAX_SIGNATURES_BODY_SIZE)

    body = DeltaSignaturesRequest.model_validate_json(body_bytes)

    if not get_delta_enabled():
        return delta_disabled_response()
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
    # ``sanitize_signature_list`` rebuilds every entry through
    # ``sanitize_block_signature``, so it already returns fresh validated dicts:
    # the previous ``[dict(s) for s in body.signatures]`` pre-copy duplicated the
    # whole list for nothing (+193 B/signature measured, up to
    # DELTA_MAX_SIGNATURES = 100_000 entries).
    clean_sigs = sanitize_signature_list(body.signatures)
    if clean_sigs is None:
        raise ValidationError(message="Invalid delta signatures payload")

    sigs = [BlockSignature(**s) for s in clean_sigs]
    # The dict list exists only to build ``sigs``: release it before matching so
    # the peak holds the matcher's representation, not both.
    del clean_sigs

    new_size = Path(file).stat().st_size
    new_sha256 = await get_or_compute_sha256(file)

    literal_path = delta_tmp_dir() / f"delta_literals_{uuid.uuid4().hex}.bin"
    match_start = time.time()

    def _match() -> tuple[list, int, bool]:
        # The literals stream straight to the temp file (never a RAM buffer) and
        # the data-op offsets come from the bounded sink, which reports the true
        # payload size and whether the cap was hit.
        with open(literal_path, "wb") as sink_file:
            sink = BoundedLiteralSink(sink_file, DELTA_MAX_LITERAL_SIZE)
            ops = find_matches_file_to_sink(file, sigs, body.block_size, sink)
        return ops, sink.total, sink.over_limit

    ops, literal_size, capped = await asyncio.to_thread(_match)
    match_duration = time.time() - match_start

    if capped:
        literal_path.unlink(missing_ok=True)
        log.info(f"Delta not worthwhile for {body.path}: literal_payload_too_large")

        record_delta_transfer("download", "rejected")
        record_transfer_duration("download", "total", time.time() - start_time)

        return JSONResponse(
            status_code=200,
            content={"delta": False, "reason": "literal_payload_too_large"},
        )

    # The base file size as seen by the client: covered span of its signatures.
    base_size = max((s.offset + s.length for s in sigs), default=0)
    ratio = changed_block_ratio(ops, base_size, body.block_size)

    # Record change ratio metric
    record_change_ratio("download", ratio)

    if not is_delta_worthwhile(ops, base_size, literal_size, body.block_size):
        # Only the change-ratio branch can trigger here: the literal cap is
        # enforced by the bounded sink while the match runs.
        literal_path.unlink(missing_ok=True)
        log.info(
            f"Delta not worthwhile for {body.path}: too_many_changed_blocks (ratio={ratio:.2f})"
        )

        record_delta_transfer("download", "rejected")
        record_transfer_duration("download", "total", time.time() - start_time)

        return JSONResponse(
            status_code=200,
            content={
                "delta": False,
                "reason": "too_many_changed_blocks",
                "changed_percentage": round(ratio, 4),
            },
        )

    # Optionally compress the literal payload (served with Content-Encoding).
    # The shared compressor is reused: ZstdCompressor compress is stateless, so
    # a new instance (with its expensive table/context construction) is never
    # needed per request.
    compressed = False
    payload_path = literal_path
    if literal_size and get_compression_enabled() and should_compress(body.path, literal_size):
        # Second pass: the sink must hold raw literals (the data ops reference
        # its offsets), so compression streams the temp file into a new one
        # instead of materialising a bytes copy of it.
        compressed_path = delta_tmp_dir() / f"delta_literals_{uuid.uuid4().hex}.zst"
        try:
            await asyncio.to_thread(
                compress_file_to_file,
                str(literal_path),
                str(compressed_path),
                level=COMPRESSION_LEVEL,
            )
        except Exception:
            literal_path.unlink(missing_ok=True)
            compressed_path.unlink(missing_ok=True)
            raise
        literal_path.unlink(missing_ok=True)
        payload_path = compressed_path
        compressed = True

    try:
        # The payload is adopted by moving the temp file into place (same
        # directory, so os.replace is atomic): it is never read into RAM.
        token = await asyncio.to_thread(
            delta_token_store.issue_from_file,
            bucket=bucket,
            payload_file=str(payload_path),
            size=literal_size,
            compressed=compressed,
            account_id=ctx.id,
        )
    except Exception:
        payload_path.unlink(missing_ok=True)
        raise

    # Record successful transfer and bytes saved
    record_delta_transfer("download", "success")
    record_transfer_duration("download", "match", match_duration)
    record_transfer_duration("download", "total", time.time() - start_time)
    # Bytes saved = original size - literal size (what we're actually sending)
    bytes_saved = max(0, new_size - literal_size)
    record_bytes_saved("download", bytes_saved)

    log.info(
        f"Delta download prepared for {body.path}: {len(ops)} ops, "
        f"{literal_size} literal bytes (ratio={ratio:.2f})"
    )
    return JSONResponse(
        status_code=200,
        content=delta_download_payload(
            block_size=body.block_size,
            new_sha256=new_sha256,
            new_size=new_size,
            ops=ops,
            data_url=rest_delta_data_path(bucket, token),
            data_size=literal_size,
            compressed=compressed,
        ),
    )
