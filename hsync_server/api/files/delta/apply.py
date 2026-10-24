# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

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
from os import remove, replace
from pathlib import Path
from shutil import copy2
from typing import Annotated, Any, Literal, cast

import zstandard as zstd
from aiofiles.ospath import isfile
from aiofiles.tempfile import NamedTemporaryFile
from fastapi import Depends, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse
from hsync_common.clock import clamp_incoming_corrected_mtime
from hsync_common.constants import DELTA_MAX_LITERAL_SIZE, PARTIAL_DELTA_SUFFIX
from hsync_common.delta import DeltaError, apply_delta
from hsync_common.format import format_duration
from hsync_common.hashing import compute_sha256_async, is_sha256_hex
from hsync_common.models.delta import (
    DeltaOp,
    sanitize_op_list,
    validate_op_bounds,
)
from hsync_common.models.event import Event, EventName, is_valid_cid
from hsync_common.models.version import relpath_in_bucket
from hsync_common.permissions import is_valid_octal_permissions
from hsync_common.protocol.rest import REST_DELTA_APPLY_SUFFIX, rest_bucket_template
from hsync_common.time_utils import conflict_timestamp
from loguru import logger as log

from hsync_server.api.authz import check_bucket_access
from hsync_server.api.connection_manager import bucket_manager
from hsync_server.api.file_utils import update_file_timestamp
from hsync_server.api.files._conflicts import plan_conflict_rejection
from hsync_server.api.files.delta._util import delta_disabled_response
from hsync_server.api.files.upload import _flush_to_disk
from hsync_server.api.files.version_publish import record_published_version
from hsync_server.api.utils import (
    enforce_size_cap,
    ensure_disk_space,
    resolve_within_bucket,
    set_permissions,
)
from hsync_server.arbitration import (
    ArbitrationOutcome,
    arbitrate_upload,
    conflicted_copy_name,
)
from hsync_server.auth import AuthContext, verify_api_key
from hsync_server.compression import decompress_file_to_file
from hsync_server.config import get_buckets_path, get_delta_enabled, get_max_upload_size
from hsync_server.exceptions import (
    ConflictLostError,
    InsufficientStorageError,
    InvalidBucketNameError,
    InvalidPathError,
    SupersededError,
    UploadError,
    ValidationError,
)
from hsync_server.exceptions import (
    FileNotFoundError as HSyncFileNotFoundError,
)
from hsync_server.file_lock_manager import get_file_lock_manager
from hsync_server.hashes import get_or_compute_sha256, get_sha256_hash
from hsync_server.metrics import (
    record_bytes_saved,
    record_delta_transfer,
    record_transfer_duration,
)
from hsync_server.rate_limiter import RATE_LIMIT_DELTA, limiter
from hsync_server.routers import auth_router


def _unprocessable(detail: str, **extra: Any) -> JSONResponse:
    """422 response for malformed instructions or failed verification."""
    return JSONResponse(status_code=422, content={"detail": detail, **extra})


def _record_delta_upload(start_time: float, status: str) -> None:
    """Record a delta upload outcome and its total duration."""
    record_delta_transfer("upload", status)
    record_transfer_duration("upload", "total", time.time() - start_time)


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
            type=cast(Literal["copy", "data"], o["type"]),
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
    return validate_op_bounds(
        ops,
        base_size=base_size,
        literal_size=literal_size,
        new_size=new_size,
    )


@auth_router.post(rest_bucket_template(REST_DELTA_APPLY_SUFFIX))
@limiter.limit(RATE_LIMIT_DELTA)
async def delta_apply(
    request: Request,  # Required by rate limiter
    bucket: str,
    ctx: Annotated[AuthContext, Depends(verify_api_key)],
    path: str = Form(...),
    metadata: str = Form(...),
    file: UploadFile = File(...),
) -> JSONResponse:
    """Apply a client-computed delta to the server-side base file."""
    check_bucket_access(bucket, ctx)
    start_time = time.time()

    if not get_delta_enabled():
        return delta_disabled_response()

    literal_tmp: str | None = None
    decompressed_tmp: str | None = None
    part_path: str | None = None
    try:
        # The final component is NOT dereferenced: the base file is read and
        # then replaced atomically. A symlink at the endpoint is a structural
        # entry that delta transfer cannot patch (no analogue does — rsync/git
        # recreate links, never patch them); refuse it explicitly so the
        # rebuild below can never turn a link into a regular file.
        file_location = resolve_within_bucket(bucket, path, resolve_final=False)
        if file_location is None:
            raise InvalidPathError(details={"path": path})
        if Path(file_location).is_symlink():
            raise ValidationError(
                message="Delta apply does not support symlink paths",
                details={"path": path},
            )
        if not await isfile(file_location):
            raise HSyncFileNotFoundError(details={"bucket": bucket, "filename": path})

        meta = _parse_metadata(metadata)
        base_sha256, new_sha256, new_size, ops = _validate_instructions(meta)

        # The rebuild materialises `new_size` bytes on disk: enforce the same
        # size cap and free-space reserve as a full upload. Without this, N copy
        # ops (all <= base_size) can declare an arbitrarily large new_size and
        # amplify a small request into a disk-exhausting write.
        enforce_size_cap(new_size, get_max_upload_size(), label="Delta result")
        ensure_disk_space(new_size)

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
            current_base = await get_or_compute_sha256(file_location)
            if current_base == new_sha256:
                # Idempotent apply: a concurrent upload/apply already stored the
                # exact desired content. Return success instead of rewriting the
                # file (and instead of a spurious 409 when the base moved).
                record_delta_transfer("upload", "already_applied")
                log.info(f"Delta apply already satisfied for {path} ({new_size} bytes)")
                return JSONResponse(
                    status_code=200,
                    content={
                        "status": "ok",
                        "sha256": new_sha256,
                        "size": new_size,
                        "resolution": "already_applied",
                    },
                )
            if current_base != base_sha256:
                log.info(f"Delta apply rejected (base changed): {file_location}")

                _record_delta_upload(start_time, "rejected")

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

            def _rebuild(target: str) -> int:
                with open(literal_tmp, "rb") as literal_stream:
                    return apply_delta(file_location, ops, literal_stream, target)

            await asyncio.to_thread(_rebuild, part_path)
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

            # 4. Causal arbitration: compare the rebuilt version against the
            # stored one (same contract as full upload, ADR-0022).
            buckets_path = get_buckets_path()
            local_bucket = f"{buckets_path}/{bucket}"
            delta_relpath = relpath_in_bucket(file_location, local_bucket) or ""
            delta_disk_hash = get_sha256_hash(file_location)
            # The client sends the causal clock as a JSON *string* (same wire
            # contract as the full upload form field). Decode it before the
            # arbitration: parse_vclock only accepts a dict, so passing the raw
            # string would silently yield an empty clock and mark every delta
            # "superseded" by the stored version.
            raw_vclock = meta.get("vclock")
            if isinstance(raw_vclock, str) and raw_vclock:
                try:
                    incoming_vclock_raw = json.loads(raw_vclock)
                except (ValueError, TypeError):
                    incoming_vclock_raw = None
            elif isinstance(raw_vclock, dict):
                incoming_vclock_raw = raw_vclock
            else:
                incoming_vclock_raw = None

            raw_corrected = meta.get("corrected_mtime")
            incoming_corrected = (
                int(clamp_incoming_corrected_mtime(float(raw_corrected)))
                if isinstance(raw_corrected, int)
                and not isinstance(raw_corrected, bool)
                and raw_corrected > 0
                else 0
            )
            delta_result = arbitrate_upload(
                bucket=bucket,
                relpath=delta_relpath,
                incoming_hash=rebuilt_sha256,
                incoming_vclock_raw=incoming_vclock_raw,
                incoming_version_id=str(meta.get("version_id") or ""),
                incoming_origin_cid=cid if is_valid_cid(cid) else None,
                disk_hash=delta_disk_hash,
                incoming_corrected_mtime=incoming_corrected,
            )

            rejection = plan_conflict_rejection(
                delta_result,
                kind="Delta upload",
                final_path=file_location,
                sha_prefix=rebuilt_sha256[:12],
            )
            if rejection is not None:
                if rejection.conflicted_path is not None:
                    # Preserve the incoming bytes under the FINAL path name: the
                    # `.delta.part` artifact must never leak into the synced tree
                    # (ADR-0021 canonical `<stem>_conflicted_copy_…`).
                    replace(part_path, rejection.conflicted_path)
                elif part_path and Path(part_path).exists():
                    remove(part_path)
                part_path = None
                raise rejection.error

            # 5. Publish: timestamps/permissions, atomic rename, cache, broadcast.
            resolution = delta_result.outcome.value
            if (
                delta_result.outcome is ArbitrationOutcome.accepted_conflict_won
                and Path(file_location).exists()
            ):
                ts = conflict_timestamp()
                conflicted = conflicted_copy_name(file_location, delta_result.loser_cid, ts)
                # Preserve the previous (losing) content before overwriting.
                copy2(file_location, conflicted)
                log.warning(
                    f"Delta conflict on {path}: previous version preserved as "
                    f"{Path(conflicted).name}"
                )

            update_file_timestamp(part_path, str(meta.get("mtime") or ""))
            if permissions:
                await set_permissions(part_path, permissions)
            replace(part_path, file_location)
            part_path = None
            src = await record_published_version(
                bucket=bucket,
                local_bucket=local_bucket,
                file_location=file_location,
                base_hash=rebuilt_sha256,
                version_id=str(meta.get("version_id") or "") or None,
                origin_cid=cid if is_valid_cid(cid) else None,
                arbitration=delta_result,
            )
            if src:
                manager = bucket_manager.get_manager(bucket)
                await manager.send_message(
                    Event(cid, local_bucket, bucket, EventName.modified, src)
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
                content={
                    "status": "ok",
                    "sha256": rebuilt_sha256,
                    "size": new_size,
                    "resolution": resolution,
                },
            )
    except (
        InvalidBucketNameError,
        InvalidPathError,
        HSyncFileNotFoundError,
        SupersededError,
        ConflictLostError,
        InsufficientStorageError,
        ValidationError,
    ):
        _record_delta_upload(start_time, "failure")
        raise
    except DeltaError as exc:
        _record_delta_upload(start_time, "failure")
        return _unprocessable("delta_apply_failed", error=exc.message)
    except Exception as exc:
        _record_delta_upload(start_time, "failure")
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
