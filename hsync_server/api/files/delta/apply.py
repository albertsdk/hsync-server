# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
Delta apply endpoint (upload delta): rebuild a file from client instructions.

The client uploads ``multipart/form-data`` with the bucket-relative ``path``,
a JSON ``metadata`` part (delta instructions + expected hashes) and the
binary literal payload. The server:

1. verifies ``base_sha256`` against the current file (409 on race),
2. validates every op against base/literal bounds and total coverage (422),
3. rebuilds into a ``.delta.part`` sibling and verifies the final SHA-256
   (422 on mismatch — the file is never corrupted in place),
4. atomically renames, then updates hash cache, tombstones and broadcasts the
   ``modified`` event — the same post-conditions as a full upload.
"""

from __future__ import annotations

import asyncio
import json
import time
from os import replace
from pathlib import Path
from typing import Any

import zstandard as zstd
from aiofiles.ospath import isfile
from aiofiles.tempfile import NamedTemporaryFile
from fastapi import File, Form, Request, UploadFile
from fastapi.responses import JSONResponse
from hsync_common.constants import DELTA_MAX_LITERAL_SIZE, PARTIAL_DELTA_SUFFIX
from hsync_common.delta import DeltaError, apply_delta
from hsync_common.format import format_duration
from hsync_common.hashing import compute_sha256_async, is_sha256_hex
from hsync_common.models.delta import (
    OP_TYPE_COPY,
    OP_TYPE_DATA,
    DeltaOp,
    sanitize_op_list,
)
from hsync_common.models.event import Event, EventName, is_valid_cid
from hsync_common.models.file import File as LocalFile
from hsync_common.models.file import invalidate_sha256_cache, is_hidden_file
from hsync_common.permissions import is_valid_octal_permissions
from loguru import logger as log

from hsync_server.api.connection_manager import bucket_manager
from hsync_server.api.file_utils import update_file_timestamp
from hsync_server.api.files.upload import _flush_to_disk
from hsync_server.api.utils import is_valid_bucket, resolve_within_bucket, set_permissions
from hsync_server.compression import decompress_file_to_file
from hsync_server.config import get_buckets_path, get_delta_enabled
from hsync_server.exceptions import (
    FileNotFoundError as HSyncFileNotFoundError,
)
from hsync_server.exceptions import (
    InvalidBucketNameError,
    InvalidPathError,
    UploadError,
    ValidationError,
)
from hsync_server.file_lock_manager import get_file_lock_manager
from hsync_server.hashes import get_sha256_hash, set_sha256_hash
from hsync_server.rate_limiter import RATE_LIMIT_DELTA, limiter
from hsync_server.routers import auth_router
from hsync_server.tombstones import clear_deletion


def _delta_disabled() -> JSONResponse:
    """404 response when the feature is off — same as a pre-delta peer."""
    return JSONResponse(status_code=404, content={"detail": "Not Found"})


def _unprocessable(detail: str, **extra: Any) -> JSONResponse:
    """422 response for malformed instructions or failed verification."""
    return JSONResponse(status_code=422, content={"detail": detail, **extra})


def _parse_metadata(raw: str) -> dict[str, Any]:
    """Parse and shape-check the JSON metadata part."""
    try:
        meta = json.loads(raw)
    except ValueError as exc:
        raise ValidationError(message="Invalid delta metadata JSON") from exc
    if not isinstance(meta, dict):
        raise ValidationError(message="Invalid delta metadata shape")
    return meta


def _validate_instructions(meta: dict[str, Any]) -> tuple[str, str, int, list[DeltaOp]]:
    """Extract and validate the instruction fields from the metadata part."""
    base_sha256 = meta.get("base_sha256")
    new_sha256 = meta.get("new_sha256")
    new_size = meta.get("new_size")
    if not is_sha256_hex(base_sha256) or not is_sha256_hex(new_sha256):
        raise ValidationError(message="Invalid SHA-256 in delta metadata")
    if not isinstance(new_size, int) or isinstance(new_size, bool) or new_size < 0:
        raise ValidationError(message="Invalid new_size in delta metadata")
    ops = sanitize_op_list(meta.get("ops"))
    if ops is None:
        raise ValidationError(message="Invalid ops in delta metadata")
    parsed = [
        DeltaOp(
            type=o["type"],
            offset=o["offset"],
            length=o["length"],
            data_offset=o["data_offset"],
        )
        for o in ops
    ]
    # Type narrowing: after validation, these are guaranteed to be the correct types
    assert isinstance(base_sha256, str)
    assert isinstance(new_sha256, str)
    return base_sha256, new_sha256, new_size, parsed


def _check_ops_bounds(
    ops: list[DeltaOp], base_size: int, literal_size: int, new_size: int
) -> str | None:
    """Validate op bounds and coverage; return a reason string or None."""
    total = 0
    for op in ops:
        if op.type == OP_TYPE_COPY:
            if op.offset + op.length > base_size:
                return "copy_op_out_of_bounds"
        elif op.type == OP_TYPE_DATA:
            if op.data_offset + op.length > literal_size:
                return "data_op_out_of_bounds"
        else:
            return "unknown_op_type"
        total += op.length
    if total != new_size:
        return "ops_coverage_mismatch"
    return None


@auth_router.post("/api/v1/buckets/{bucket}/files/delta/apply")
@limiter.limit(RATE_LIMIT_DELTA)
async def delta_apply(
    request: Request,  # Required by rate limiter
    bucket: str,
    path: str = Form(...),
    metadata: str = Form(...),
    file: UploadFile = File(...),
) -> JSONResponse:
    """Apply a client-computed delta to the server-side base file."""
    start_time = time.time()

    if not get_delta_enabled():
        return _delta_disabled()

    literal_tmp: str | None = None
    decompressed_tmp: str | None = None
    part_path: str | None = None
    try:
        if not is_valid_bucket(bucket):
            raise InvalidBucketNameError(details={"bucket": bucket})
        file_location = resolve_within_bucket(bucket, path)
        if file_location is None:
            raise InvalidPathError(details={"path": path})
        if not await isfile(file_location):
            raise HSyncFileNotFoundError(details={"bucket": bucket, "filename": path})

        meta = _parse_metadata(metadata)
        base_sha256, new_sha256, new_size, ops = _validate_instructions(meta)

        cid = meta.get("cid", "delta")
        if not is_valid_cid(cid):
            raise ValidationError(message="Invalid client ID format", details={"cid": cid})
        permissions = meta.get("permissions")
        if permissions and not is_valid_octal_permissions(permissions):
            raise ValidationError(
                message="Invalid permission format", details={"permissions": permissions}
            )

        # Acquire per-file lock to serialize concurrent delta applies on the same file.
        # This prevents race conditions where multiple applies would write to the same
        # .delta.part file simultaneously.
        lock_manager = get_file_lock_manager()
        async with lock_manager.acquire(file_location):
            # 1. Race check: the base file must still be what the client saw.
            # This check MUST be inside the lock to prevent TOCTOU races.
            current_base = get_sha256_hash(file_location) or await compute_sha256_async(
                file_location
            )
            if current_base != base_sha256:
                log.info(f"Delta apply rejected (base changed): {file_location}")

                # Record metrics for rejected transfer
                from hsync_server.metrics import record_delta_transfer, record_transfer_duration

                record_delta_transfer("upload", "rejected")
                record_transfer_duration("upload", "total", time.time() - start_time)

                return JSONResponse(
                    status_code=409,
                    content={
                        "detail": "base_file_changed",
                        "expected_base_sha256": base_sha256,
                        "actual_base_sha256": current_base,
                    },
                )

            # Park the literal payload on disk (streamed, never fully in RAM).
            file_directory = str(Path(file_location).parent)
            async with NamedTemporaryFile(
                delete=False, dir=file_directory, prefix=".hsync-delta-lit-"
            ) as tmp:
                literal_tmp = str(tmp.name)
                total = 0
                while chunk := await file.read(8192):
                    total += len(chunk)
                    if total > DELTA_MAX_LITERAL_SIZE:
                        raise ValidationError(
                            message="Literal payload exceeds maximum size",
                            details={"max": DELTA_MAX_LITERAL_SIZE},
                        )
                    await tmp.write(chunk)
                await _flush_to_disk(tmp)

            literal_size = total
            if meta.get("compressed") is True:
                # Anti decompression-bomb: expansion is hard-capped.
                decompressed_tmp = f"{literal_tmp}.decompressed"
                try:
                    literal_size = await asyncio.to_thread(
                        decompress_file_to_file,
                        literal_tmp,
                        decompressed_tmp,
                        DELTA_MAX_LITERAL_SIZE,
                    )
                except zstd.ZstdError as exc:
                    raise ValidationError(message=f"Invalid compressed payload: {exc}") from exc
                replace(decompressed_tmp, literal_tmp)
                decompressed_tmp = None

            # 2. Validate ops against the real bounds before touching anything.
            base_size = Path(file_location).stat().st_size
            invalid = _check_ops_bounds(ops, base_size, literal_size, new_size)
            if invalid is not None:
                return _unprocessable(invalid)

            # 3. Rebuild into a .delta.part sibling and verify the final hash.
            part_path = f"{file_location}{PARTIAL_DELTA_SUFFIX}"

            rebuild_start = time.time()

            def _rebuild() -> int:
                with open(literal_tmp, "rb") as literal_stream:
                    return apply_delta(file_location, ops, literal_stream, part_path)

            await asyncio.to_thread(_rebuild)
            rebuild_duration = time.time() - rebuild_start

            rebuilt_sha256 = await compute_sha256_async(part_path)
            if rebuilt_sha256 != new_sha256:
                return _unprocessable(
                    "reconstructed_file_hash_mismatch",
                    expected_sha256=new_sha256,
                    actual_sha256=rebuilt_sha256,
                )
            if Path(part_path).stat().st_size != new_size:
                return _unprocessable("reconstructed_file_size_mismatch")

            # 4. Publish: timestamps/permissions, atomic rename, cache, broadcast.
            update_file_timestamp(part_path, None, str(meta.get("mtime") or ""))
            if permissions:
                await set_permissions(part_path, permissions)
            replace(part_path, file_location)
            part_path = None
            set_sha256_hash(file_location, rebuilt_sha256)
            invalidate_sha256_cache(file_location)

            buckets_path = get_buckets_path()
            clear_deletion(bucket, f"{buckets_path}/{bucket}", file_location)

            src = None if is_hidden_file(file_location) else LocalFile().get(file_location)
            if src:
                manager = bucket_manager.get_manager(bucket)
                await manager.send_message(
                    Event(cid, f"{buckets_path}/{bucket}", bucket, EventName.modified, src)
                )

            # Record metrics for successful transfer
            from hsync_server.metrics import (
                record_bytes_saved,
                record_delta_transfer,
                record_transfer_duration,
            )

            record_delta_transfer("upload", "success")
            record_transfer_duration("upload", "apply", rebuild_duration)
            record_transfer_duration("upload", "total", time.time() - start_time)
            # Bytes saved = new file size - literal size (what was actually uploaded)
            bytes_saved = max(0, new_size - literal_size)
            record_bytes_saved("upload", bytes_saved)

            log.info(
                f"Delta apply completed for {path} ({new_size} bytes) "
                f"{format_duration(time.time() - start_time, subsecond=True)}"
            )
            return JSONResponse(
                status_code=200,
                content={"status": "ok", "sha256": rebuilt_sha256, "size": new_size},
            )
    except (
        InvalidBucketNameError,
        InvalidPathError,
        HSyncFileNotFoundError,
        ValidationError,
    ):
        # Record metrics for failed transfer
        from hsync_server.metrics import record_delta_transfer, record_transfer_duration

        record_delta_transfer("upload", "failure")
        record_transfer_duration("upload", "total", time.time() - start_time)
        raise
    except DeltaError as exc:
        # Record metrics for failed transfer
        from hsync_server.metrics import record_delta_transfer, record_transfer_duration

        record_delta_transfer("upload", "failure")
        record_transfer_duration("upload", "total", time.time() - start_time)
        return _unprocessable("delta_apply_failed", error=exc.message)
    except Exception as exc:
        # Record metrics for failed transfer
        from hsync_server.metrics import record_delta_transfer, record_transfer_duration

        record_delta_transfer("upload", "failure")
        record_transfer_duration("upload", "total", time.time() - start_time)
        log.error(f"Delta apply failed for {path}: {exc}", exc_info=True)
        raise UploadError(
            message="Delta apply failed",
            details={"path": path, "error": type(exc).__name__},
        ) from exc
    finally:
        for leftover in (literal_tmp, decompressed_tmp, part_path):
            if leftover and Path(leftover).exists():
                try:
                    Path(leftover).unlink()
                except OSError:
                    log.warning(f"Cannot remove delta leftover: {leftover}")
