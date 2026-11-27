# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""
File upload endpoint for HSYNC Server.

This module provides the REST API endpoint for file upload with:
- Resumable upload support
- SHA-256 checksum verification
- Timestamp preservation
- Permission setting
- Compression support
"""

import asyncio
import hashlib
import hmac
from os import fsync, link, remove, replace
from os.path import samefile
from pathlib import Path
from shutil import copy2
from time import monotonic
from typing import Annotated, Any

import aiofiles
import zstandard as zstd
from aiofiles.tempfile import NamedTemporaryFile
from fastapi import Depends, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse
from hsync_common.clock import clamp_incoming_corrected_mtime
from hsync_common.constants import HISTORY_REASON_OVERWRITE, PARTIAL_DOWNLOAD_SUFFIX
from hsync_common.deletions import covered_by_tree_delete
from hsync_common.format import format_bytes, format_duration
from hsync_common.hashing import compute_sha256_async, is_sha256_hex
from hsync_common.ids import is_valid_cid
from hsync_common.models.event import Event, EventName
from hsync_common.models.file import is_partial_file
from hsync_common.models.version import decode_wire_vclock, relpath_in_bucket
from hsync_common.path_validation import is_internal_name, is_valid_filename
from hsync_common.permissions import is_valid_octal_permissions
from hsync_common.protocol.rest import REST_FILES_SUFFIX, rest_bucket_template
from hsync_common.time_utils import conflict_timestamp
from loguru import logger as log

from hsync_server.api.authz import ensure_bucket_access
from hsync_server.api.connection_manager import bucket_manager
from hsync_server.api.file_utils import (
    update_file_timestamp,
)
from hsync_server.api.files._conflicts import plan_conflict_rejection
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
from hsync_server.config import get_buckets_path, get_max_upload_size
from hsync_server.constants import BACKUP_SUFFIX, DEFAULT_FILE_PERMISSIONS, ActionLabel
from hsync_server.exceptions import (
    ChecksumMismatchError,
    ConflictError,
    FileTooLargeError,
    InvalidFilenameError,
    InvalidPathError,
    InvalidPermissionFormatError,
    UploadError,
    ValidationError,
)
from hsync_server.file_lock_manager import get_file_lock_manager
from hsync_server.hashes import get_sha256_hash, set_sha256_hash
from hsync_server.history import capture_file
from hsync_server.rate_limiter import RATE_LIMIT_UPLOAD, limiter
from hsync_server.routers import auth_router
from hsync_server.tombstones import (
    clear_deletion,
    deletion_marker_snapshot,
    destination_mutation_since,
)

from .permissions import apply_metadata_to_existing_file
from .version_publish import record_published_version

# Chunk size for copying the spooled upload body to its destination file.
# 8 KiB made the copy loop event-loop-bound on large files (one await per
# 8 KiB); 256 KiB matches the transfer chunk tiers used across the ecosystem.
_UPLOAD_COPY_CHUNK = 256 * 1024


async def _flush_to_disk(handle) -> None:
    """Flush an open aiofiles handle's buffers to physical storage (fsync)."""
    await handle.flush()
    await asyncio.to_thread(fsync, handle.fileno())


def _backup_existing_file(file_location: str, backup_path: str) -> None:
    """Snapshot an existing file to *backup_path* before it is overwritten.

    Prefers a hard link (O(1), same inode) so a large overwrite no longer pays a
    full byte-for-byte copy: the publish step uses an atomic ``replace`` onto a
    new temp inode, so the snapshot keeps the previous content and the two never
    alias. A crash between the link and the publish can leave *backup_path*
    pointing at the same inode as the target; that stale link is reused as the
    snapshot (removing it would delete the target's last other name, and copying
    a file onto itself raises ``SameFileError``). Falls back to :func:`copy2`
    when the filesystem does not support links.
    """
    try:
        link(file_location, backup_path)
        return
    except FileExistsError:
        try:
            if samefile(file_location, backup_path):
                return  # already a snapshot of the current inode
        except OSError:
            pass
        try:
            remove(backup_path)
            link(file_location, backup_path)
            return
        except OSError:
            pass
    except OSError:
        pass
    copy2(file_location, backup_path)


def _wire_relpath(bucket: str, path: str, filename: str | None) -> str:
    """Bucket-relative path for wire responses and logs.

    Never leaks the absolute filesystem path (the client already knows the
    bucket-relative path it uploaded) and avoids a double slash when ``path``
    is empty (root upload). ``filename`` may be ``None`` for a malformed
    multipart part and is rendered as-is, matching the previous f-string.
    """
    return f"{bucket}/{path}/{filename}" if path else f"{bucket}/{filename}"


def _partial_upload_response(total_bytes: int, size: int) -> JSONResponse:
    """202 response for a chunked upload that still expects more chunks."""
    return JSONResponse(
        status_code=202,
        content={
            "info": "Partial upload received",
            "offset": total_bytes,
            "remaining": size - total_bytes,
        },
    )


def _resync_upload_response(offset: int, size: int) -> JSONResponse:
    """202 response for a preflight offset that no longer matches the .part.

    Mirrors the P2P master's resync contract: the .part has a different size
    than the offset the client probed (a duplicate writer appended bytes, or
    the .part is gone because the completing writer renamed it). The client's
    body is never written; the peer re-probes ``upload-status`` and resumes
    from the reported offset.
    """
    return JSONResponse(
        status_code=202,
        content={
            "info": "Partial upload offset resync",
            "offset": offset,
            "remaining": max(0, size - offset),
        },
    )


def _validate_upload_request(
    *,
    cid: str,
    file: UploadFile,
    size: int,
    offset: int,
    compressed: str,
    max_upload_size: int,
    sha256sum: str,
    permissions: str,
    is_compressed: bool,
    is_resumable: bool,
) -> str:
    """Validate the upload metadata before any body is read; return the used name.

    Every rejection here happens before the body is streamed, so an invalid
    request is cheap and never writes a temp file (DoS guard). The declared
    checksum and CID must be well-formed and the sanitized filename must be
    allowed; internal sync-engine artifacts are refused so a peer cannot alter
    another peer's sync behaviour through the wire.
    """
    if max_upload_size > 0 and size > max_upload_size:
        raise FileTooLargeError(
            message=f"File size {size} exceeds maximum allowed size of {max_upload_size} bytes",
            details={
                "max_size": max_upload_size,
                "provided_size": size,
            },
        )

    # Check disk space before accepting upload (size-aware: the filesystem
    # must hold the configured reserve plus the incoming file)
    ensure_disk_space(size)

    if not is_valid_cid(cid):
        raise ValidationError(
            message="Invalid client ID format",
            details={"cid": cid},
        )
    # The declared checksum must be a well-formed digest: it is compared
    # with hmac.compare_digest, which raises TypeError on non-ASCII input
    # — a malformed declaration must be a 400, not an unhandled 500.
    if not is_sha256_hex(sha256sum):
        raise ValidationError(
            message="Invalid SHA-256 checksum format",
            details={"sha256sum": sha256sum},
        )
    # Compressed uploads must arrive in a single request: the resumable
    # path appends raw bytes to a .part and never decompresses it, so a
    # resumed compressed upload could only ever fail the integrity check.
    if is_compressed and is_resumable:
        raise ValidationError(
            message=(
                "Compressed uploads are not resumable: send the full payload in a single request"
            ),
            details={"offset": offset, "compressed": compressed},
        )
    if permissions and not is_valid_octal_permissions(permissions):
        raise InvalidPermissionFormatError(details={"permissions": permissions})

    # A name that any supported device cannot store identically must not enter
    # the shared namespace: renaming it here would make this copy disagree with
    # the sender's (and with every other peer), so the request is refused and
    # the sender reports the item instead.
    original_filename = file.filename or ""
    if not is_valid_filename(original_filename):
        raise InvalidFilenameError(
            message=(
                "The file name cannot be stored identically on every device "
                '(remove < > : " / \\ | ? *, trailing dots/spaces, and reserved '
                "names such as CON)"
            ),
            details={"filename": original_filename},
        )

    # Internal sync-engine artifacts must never enter the storage via the
    # wire: a peer that could (re)write them would alter every other
    # peer's sync behaviour through the sync channel itself.
    if is_internal_name(original_filename):
        raise InvalidFilenameError(
            message="Internal sync-engine filenames are not allowed",
            details={"filename": original_filename},
        )
    return original_filename


async def _upload_duplicate_shortcut(
    *,
    bucket: str,
    path: str,
    file: UploadFile,
    cid: str,
    sha256sum: str,
    mtime: str,
    permissions: str,
    file_location: str,
    partial_file_path: str,
    buckets_path: str,
) -> JSONResponse | None:
    """Short-circuit an upload whose content is already on disk (identical SHA-256).

    A permission-only change (chmod) reuses this branch: the content is already
    on disk, so instead of rejecting the upload the new mode is applied and the
    other connected clients are notified. Returns ``None`` when there is nothing
    to short-circuit (new content, or no declared checksum).
    """
    if not (
        sha256sum
        and Path(file_location).exists()
        and hmac.compare_digest(sha256sum, get_sha256_hash(file_location) or "")
    ):
        return None
    log.warning(f"File already present: {file_location}")
    # Update mtime even if skipping upload
    update_file_timestamp(file_location, mtime)
    # The utime pass changed the (mtime_ns) the cache keyed on: refresh
    # the entry so the next identical upload still hits the dup-skip.
    set_sha256_hash(file_location, sha256sum)
    # The file exists: drop any stale deletion marker for it.
    clear_deletion(bucket, f"{buckets_path}/{bucket}", file_location)
    # Clean up partial file if exists
    if Path(partial_file_path).exists():
        remove(partial_file_path)
    # Shared metadata-only branch (same logic as the dedicated
    # PATCH /files/permissions endpoint): applies the new mode and/or
    # mtime when it diverges and re-broadcasts a causal `modified` event.
    permissions_only = await apply_metadata_to_existing_file(
        bucket=bucket,
        file_location=file_location,
        cid=cid,
        permissions=permissions,
    )
    # Never leak absolute filesystem paths on the wire — the
    # client already knows the bucket-relative path it uploaded.
    if permissions_only:
        return JSONResponse(
            status_code=200,
            content={
                "info": "Permissions updated",
                "file": _wire_relpath(bucket, path, file.filename),
            },
        )
    return JSONResponse(
        status_code=409,
        content={
            "info": "File already present",
            "file": _wire_relpath(bucket, path, file.filename),
        },
    )


async def _expand_compressed_upload_temp(
    file_location_tmp: str,
    *,
    declared_size: int,
    compressed_bytes: int,
    max_upload_size: int,
) -> int:
    """Expand the compressed upload temp onto itself; return the decompressed size.

    The expansion is hard-capped (anti decompression-bomb) and streamed through a
    worker thread, so neither the compressed input nor the expanded output (each
    up to the cap) is ever fully buffered in memory.
    """
    if declared_size > 0:
        max_decompressed = declared_size
    elif max_upload_size > 0:
        max_decompressed = max_upload_size
    else:
        raise ValidationError(
            message="Missing/invalid 'size' for compressed upload",
            details={"size": declared_size},
        )
    decompressed_tmp = f"{file_location_tmp}.decompressed"
    try:
        try:
            decompressed_bytes = await asyncio.to_thread(
                decompress_file_to_file,
                file_location_tmp,
                decompressed_tmp,
                max_decompressed,
            )
        except zstd.ZstdError as exc:
            raise ValidationError(
                message=f"Invalid compressed payload: {exc}",
                details={"size": declared_size, "compressed_bytes": compressed_bytes},
            ) from exc
        if declared_size > 0 and decompressed_bytes != declared_size:
            raise ValidationError(
                message=(
                    f"Decompressed size mismatch: expected {declared_size}, "
                    f"got {decompressed_bytes}"
                ),
                details={"expected": declared_size, "actual": decompressed_bytes},
            )
        # Swap the expanded content back onto the upload temp path
        # (same-filesystem rename).
        replace(decompressed_tmp, file_location_tmp)
        return decompressed_bytes
    finally:
        if Path(decompressed_tmp).exists():
            remove(decompressed_tmp)


async def _append_resumable_upload(
    file: UploadFile,
    partial_file_path: str,
    *,
    offset: int,
    size: int,
    max_upload_size: int,
    fused_sha: Any,
) -> tuple[int, JSONResponse | None]:
    """Append the incoming body to an existing ``.part`` under the per-path lock.

    Returns ``(total_bytes, early_response)``; ``early_response`` is a resync (202
    with the true offset) or a partial (202) response when the body is not the
    final chunk. The lock is held on the ``.part`` path so two concurrent uploads
    of one destination cannot interleave appends (the arbitration lock below keys
    on the final path, so this cannot self-deadlock).
    """
    async with get_file_lock_manager().acquire(partial_file_path):
        tmp_exists = Path(partial_file_path).exists()
        current_size = Path(partial_file_path).stat().st_size if tmp_exists else 0
        if current_size != offset:
            # A duplicate writer appended bytes, or the .part is gone (the peer
            # that completed the upload already renamed it). Keep the .part, do
            # not write the body, and answer 202 with the true offset.
            log.warning(
                f"Upload offset resync for {file.filename}: "
                f"local .part has {current_size} bytes, client offset {offset}"
            )
            return 0, _resync_upload_response(current_size, size)
        total_bytes = offset
        # Fused checksum (resume): ingest the already-received prefix so the digest
        # covers the complete file. Compressed uploads are never resumable
        # (rejected above), so fused_sha is set here.
        async with aiofiles.open(partial_file_path, "rb") as prefix:
            while prefix_chunk := await prefix.read(_UPLOAD_COPY_CHUNK):
                fused_sha.update(prefix_chunk)
        async with aiofiles.open(partial_file_path, "ab") as f:
            while chunk := await file.read(_UPLOAD_COPY_CHUNK):
                total_bytes += len(chunk)
                enforce_size_cap(total_bytes, max_upload_size)
                await f.write(chunk)
                fused_sha.update(chunk)
            # Ensure appended data reaches physical storage
            await _flush_to_disk(f)
        if size > 0 and total_bytes < size:
            # Partial upload - more chunks expected
            return total_bytes, _partial_upload_response(total_bytes, size)
        return total_bytes, None


async def _arbitrate_publish_upload(
    *,
    bucket: str,
    path: str,
    file: UploadFile,
    cid: str,
    mtime: str,
    permissions: str,
    version_id: str,
    corrected_mtime: int,
    incoming_vclock_raw: Any,
    local_bucket: str,
    file_location: str,
    file_location_tmp: str,
    backup_path: str | None,
    sha256sum_tmp: str,
    bucket_root: str,
    dest_existed: bool,
    dest_marker: Any,
) -> tuple[Any, str, Any, JSONResponse | None]:
    """Arbitrate the incoming upload against the stored version and publish it.

    Runs the whole critical section under the per-path lock (read store → decide →
    write file → record, ADR-0022 §3.4.6), so two concurrent uploads for one path
    are ordered. Returns ``(src, resolution, result, early_response)``;
    ``early_response`` is a 409 when the destination changed under the upload. A
    lost arbitration raises.
    """
    lock_manager = get_file_lock_manager()
    async with lock_manager.acquire(file_location):
        relpath = relpath_in_bucket(file_location, local_bucket) or ""
        disk_hash = get_sha256_hash(file_location) if Path(file_location).exists() else None
        # ADR-0023 receiver-side guard: a far-future peer value cannot win every
        # tie-break; it is clamped against the receiver's corrected clock.
        incoming_corrected = int(clamp_incoming_corrected_mtime(float(corrected_mtime)))
        result = arbitrate_upload(
            bucket=bucket,
            relpath=relpath,
            incoming_hash=sha256sum_tmp,
            incoming_vclock_raw=incoming_vclock_raw,
            incoming_version_id=version_id,
            incoming_origin_cid=cid if is_valid_cid(cid) else None,
            disk_hash=disk_hash,
            incoming_corrected_mtime=incoming_corrected,
        )
        rejection = plan_conflict_rejection(
            result,
            kind="Upload",
            final_path=file_location,
            sha_prefix=sha256sum_tmp[:12],
            has_backup=bool(backup_path and Path(backup_path).exists()),
        )
        if rejection is not None:
            if rejection.conflicted_path is not None:
                # Incoming loses the tie-break: preserve its bytes as a conflicted
                # copy, keep the winner on disk, reject.
                replace(file_location_tmp, rejection.conflicted_path)
            elif Path(file_location_tmp).exists():
                # Stale upload (causally dominated): discard the temp file.
                remove(file_location_tmp)
            if rejection.discard_backup and backup_path and Path(backup_path).exists():
                remove(backup_path)
            raise rejection.error
        # accepted or accepted_conflict_won: write the incoming content. On
        # accepted_conflict_won, preserve the previous (losing) content as a
        # conflicted copy using the backup created earlier.
        resolution = result.outcome.value
        if (
            result.outcome is ArbitrationOutcome.accepted_conflict_won
            and backup_path
            and Path(backup_path).exists()
        ):
            ts = conflict_timestamp()
            conflicted = conflicted_copy_name(file_location, result.loser_cid, ts)
            replace(backup_path, conflicted)
            log.warning(
                f'{ActionLabel.UPLOAD} Conflict detected on "{bucket}/{path}/{file.filename}" '
                f"(prev SHA {(disk_hash or '')[:12]}…, new SHA {sha256sum_tmp[:12]}…): "
                f'previous version preserved as "{Path(conflicted).name}"'
            )
        elif backup_path and Path(backup_path).exists():
            remove(backup_path)
        if destination_mutation_since(
            bucket, bucket_root, file_location, dest_existed, dest_marker
        ):
            log.info(
                f'Upload rejected (destination changed): "{_wire_relpath(bucket, path, file.filename)}"'
            )
            return (
                None,
                "",
                None,
                JSONResponse(status_code=409, content={"detail": "destination_changed"}),
            )
        # Atomically move file to final destination (same-filesystem rename).
        replace(file_location_tmp, file_location)
        # Metadata only on content that passed integrity verification.
        update_file_timestamp(file_location, mtime)
        if permissions:
            await set_permissions(file_location, permissions)
        # Captured while STILL holding the lock: the model must describe exactly
        # the generation this upload published, or a later same-path upload could
        # broadcast its content under this CID.
        src = await record_published_version(
            bucket=bucket,
            local_bucket=local_bucket,
            file_location=file_location,
            base_hash=sha256sum_tmp,
            version_id=version_id or None,
            origin_cid=cid if is_valid_cid(cid) else None,
            arbitration=result,
        )
        return src, resolution, result, None


async def _receive_new_upload_body(
    file: UploadFile,
    *,
    file_directory: str,
    partial_file_path: str,
    size: int,
    max_upload_size: int,
    is_compressed: bool,
    fused_sha: Any,
) -> tuple[str | None, int, JSONResponse | None]:
    """Copy a new/first-chunk body to a hidden temp file, expanding if compressed.

    Returns ``(file_location_tmp, total_bytes, early_response)``; ``early_response``
    is a 202 partial response when the body is not the final chunk (``None``
    otherwise, and ``file_location_tmp`` is a valid temp then). The temp is created
    in the destination directory so the final rename is an atomic same-filesystem
    move (the dot prefix keeps it out of /files listings).
    """
    total_bytes = 0
    # If size > 0, this might be the first chunk of a multi-chunk upload.
    if size > 0 and Path(partial_file_path).exists():
        remove(partial_file_path)
    async with NamedTemporaryFile(
        delete=False, dir=file_directory, prefix=".hsync-upload-"
    ) as temp_file:
        file_location_tmp = str(temp_file.name)
        while chunk := await file.read(_UPLOAD_COPY_CHUNK):
            total_bytes += len(chunk)
            enforce_size_cap(total_bytes, max_upload_size)
            await temp_file.write(chunk)
            if fused_sha is not None:
                fused_sha.update(chunk)
        # Ensure written data reaches physical storage
        await _flush_to_disk(temp_file)
    if is_compressed and total_bytes > 0:
        total_bytes = await _expand_compressed_upload_temp(
            file_location_tmp,
            declared_size=size,
            compressed_bytes=total_bytes,
            max_upload_size=max_upload_size,
        )
    if size > 0 and total_bytes < size and not is_compressed:
        # Move temp file to partial file location (same-filesystem rename).
        replace(file_location_tmp, partial_file_path)
        return None, total_bytes, _partial_upload_response(total_bytes, size)
    return file_location_tmp, total_bytes, None


async def _verify_upload_checksum(file_location_tmp: str, *, declared: str, fused_sha: Any) -> str:
    """Return the received digest, raising when it does not match *declared*."""
    if fused_sha is not None:
        # Uncompressed uploads: the digest was fused into the copy loop (no
        # second full read of the temp file).
        actual = fused_sha.hexdigest()
    else:
        # Compressed uploads: the fused hasher would cover the raw compressed
        # bytes — hash the finalized decompressed file.
        actual = await compute_sha256_async(file_location_tmp)
    if not hmac.compare_digest(declared, actual):
        raise ChecksumMismatchError(
            message=f"Checksum mismatch: expected {declared}, got {actual}",
            details={"expected": declared, "actual": actual},
        )
    return actual


async def _broadcast_and_respond(
    *,
    src: Any,
    cid: str,
    local_bucket: str,
    bucket: str,
    total_bytes: int,
    t0: float,
    resolution: str,
    result: Any,
    path: str,
    file: UploadFile,
) -> JSONResponse:
    """Broadcast the pre-captured metadata (outside the lock) and build the 201."""
    if src:
        manager = bucket_manager.get_manager(bucket)
        await manager.send_message(Event(cid, local_bucket, bucket, EventName.added, src))
    log.info(
        f'{ActionLabel.UPLOAD} "{_wire_relpath(bucket, path, file.filename)}" (CID: {cid}) '
        f"{format_bytes(total_bytes)} {format_duration(monotonic() - t0, subsecond=True)}"
    )
    return JSONResponse(
        status_code=201,
        content={
            "info": "File uploaded successfully",
            "file": _wire_relpath(bucket, path, file.filename),
            "resolution": resolution,
            # ADR-0023: the recorded causal version (merged clock + corrected mtime)
            # so the uploader can mirror the agreed base exactly.
            "version": {
                "vclock": result.record_vclock,
                "corrected_mtime": result.record_corrected_mtime,
            },
        },
    )


def _reject_covered_by_tree_delete(file_location: str, path: str, file: UploadFile) -> None:
    """Refuse an upload landing inside a subtree another client is removing.

    Such a file recreates what the deleting peer removed, the rmtree fails with
    ENOTEMPTY and the tree stays half present (see ``hsync_common.deletions``); the
    peer re-offers the content once the removal completed.
    """
    if not covered_by_tree_delete(file_location):
        return
    log.debug(f"[TREE-DELETE] Refused an upload inside an in-flight delete: {file_location}")
    raise ConflictError(
        message="Path is being deleted",
        details={"path": path, "filename": file.filename},
    )


@auth_router.post(rest_bucket_template(REST_FILES_SUFFIX))
@limiter.limit(RATE_LIMIT_UPLOAD)
async def upload_file(
    request: Request,  # Required by rate limiter
    bucket: str,
    ctx: Annotated[AuthContext, Depends(verify_api_key)],
    cid: str = Form(),
    file: UploadFile = File(...),
    path: str = Form(""),
    ctime: str = Form(""),
    mtime: str = Form(""),
    sha256sum: str = Form(),
    permissions: str = Form(DEFAULT_FILE_PERMISSIONS),
    offset: int = Form(0),
    size: int = Form(0),
    compressed: str = Form("false"),
    vclock: str = Form(""),  # JSON-encoded causal vector clock (ADR-0022)
    version_id: str = Form(""),  # opaque version lineage id from the uploader
    corrected_mtime: int = Form(0),  # ADR-0023 offset-corrected mtime (0 ⇒ unknown)
) -> JSONResponse:
    """
    Upload a file to a bucket with resumable upload support.

    Handles file upload with integrity verification (SHA-256), timestamp
    preservation, and permission setting. Broadcasts an "added" event
    to all connected WebSocket clients.

    Supports resumable uploads: if offset > 0, the incoming data is
    appended to an existing partial file (.part extension).

    Args:
        bucket: Target bucket name.
        cid: Client ID for event broadcasting.
        file: The file to upload.
        path: Subdirectory path within the bucket.
        ctime: Creation time as Unix epoch seconds (int) or ISO 8601 string
            (e.g. ``2025-01-15T08:30:00Z``). Naive strings are treated as UTC.
        mtime: Modification time as Unix epoch seconds (int) or ISO 8601 string
            (e.g. ``2025-01-15T08:30:00Z``). Naive strings are treated as UTC.
        sha256sum: Expected SHA-256 checksum for integrity verification.
        permissions: File permissions as an octal string (e.g., '644').
        offset: Byte offset for resumable uploads (0 for new uploads).
        size: Total file size (required for resumable uploads).

    Returns:
        JSON response confirming upload or conflict if identical file exists.

    Raises:
        InvalidBucketNameError: If bucket name is invalid.
        InvalidPathError: If path is invalid.
        InvalidPermissionFormatError: If permission format is invalid.
        InvalidFilenameError: If filename contains forbidden characters or is reserved.
        ChecksumMismatchError: If SHA-256 checksum verification fails.
            UploadError: If upload operation fails.
    """
    ensure_bucket_access(bucket, ctx)
    file_location_tmp = None
    partial_file_path = None
    backup_path = None
    file_location = None
    is_resumable = offset > 0
    is_compressed = compressed.lower() == "true"
    t0 = monotonic()
    try:
        max_upload_size = get_max_upload_size()
        buckets_path = get_buckets_path()

        file.filename = _validate_upload_request(
            cid=cid,
            file=file,
            size=size,
            offset=offset,
            compressed=compressed,
            max_upload_size=max_upload_size,
            sha256sum=sha256sum,
            permissions=permissions,
            is_compressed=is_compressed,
            is_resumable=is_resumable,
        )

        # Confine the upload directory to *this* bucket: the raw form path is
        # untrusted input and must never escape into a sibling bucket.
        file_directory = resolve_within_bucket(bucket, path)
        if file_directory is None:
            raise InvalidPathError(details={"path": path})

        # Define file paths
        file_location = f"{file_directory}/{file.filename}"
        # A subtree another client is removing right now is closed to writes.
        _reject_covered_by_tree_delete(file_location, path, file)
        # The destination is snapshotted before the body streams: if it is
        # deleted, renamed away or recreated while the upload runs, the final
        # move must not commit over it (same contract as the P2P master gate).
        bucket_root = f"{buckets_path}/{bucket}"
        dest_existed = Path(file_location).exists()
        dest_marker = deletion_marker_snapshot(bucket, bucket_root, file_location)

        partial_file_path = f"{file_location}{PARTIAL_DOWNLOAD_SUFFIX}"

        # Skip upload if identical file already exists (same SHA-256). A
        # permission-only change (chmod) reuses this branch: the content is
        # already on disk, so instead of rejecting the upload we apply the
        # new permissions and notify the other connected clients — the
        # permission change is a normal causal `modified` event.
        duplicate = await _upload_duplicate_shortcut(
            bucket=bucket,
            path=path,
            file=file,
            cid=cid,
            sha256sum=sha256sum,
            mtime=mtime,
            permissions=permissions,
            file_location=file_location,
            partial_file_path=partial_file_path,
            buckets_path=buckets_path,
        )
        if duplicate is not None:
            return duplicate

        # Ensure directory exists
        Path(file_directory).mkdir(parents=True, exist_ok=True)

        # This ensures readers can still access the complete original file
        # while the new version is being uploaded
        backup_path = f"{file_location}{BACKUP_SUFFIX}"
        if Path(file_location).exists():
            # Preserve the content this publish replaces (ADR-0057) while it is
            # still the current one: the ``.backup`` snapshot below only lives
            # until the publish finishes.
            capture_file(
                bucket,
                file_location,
                bucket_dir=str(Path(buckets_path) / bucket),
                reason=HISTORY_REASON_OVERWRITE,
                cid=cid,
            )
            _backup_existing_file(file_location, backup_path)

        # Fused checksum: updated while the body is copied so a complete
        # upload needs no second full read of the temp file (the previous
        # implementation re-read the whole file with compute_sha256_async
        # after the copy). Excluded for compressed uploads — the verified
        # hash is of the DECOMPRESSED content, which only exists after the
        # raw copy is expanded below.
        fused_sha = None if is_compressed else hashlib.sha256()

        if is_resumable:
            total_bytes, early = await _append_resumable_upload(
                file,
                partial_file_path,
                offset=offset,
                size=size,
                max_upload_size=max_upload_size,
                fused_sha=fused_sha,
            )
            if early is not None:
                return early
            # Upload complete - verify and finalize
            file_location_tmp = partial_file_path
        else:
            file_location_tmp, total_bytes, early = await _receive_new_upload_body(
                file,
                file_directory=file_directory,
                partial_file_path=partial_file_path,
                size=size,
                max_upload_size=max_upload_size,
                is_compressed=is_compressed,
                fused_sha=fused_sha,
            )
            if early is not None:
                return early

        # SHA-256 integrity is mandatory: the received bytes must hash to the
        # declared checksum, otherwise the upload is rejected (400). Verify
        # *before* touching any file metadata so rejected uploads never alter
        # existing on-disk state.
        assert file_location_tmp is not None  # a completed/partial upload holds a temp
        sha256sum_tmp = await _verify_upload_checksum(
            file_location_tmp, declared=sha256sum, fused_sha=fused_sha
        )

        # Parse the incoming causal vector clock (JSON form field; malformed ⇒
        # empty clock, handled natively by the arbitration matrix).
        incoming_vclock_raw = decode_wire_vclock(vclock)

        local_bucket = f"{buckets_path}/{bucket}"
        src, resolution, result, early = await _arbitrate_publish_upload(
            bucket=bucket,
            path=path,
            file=file,
            cid=cid,
            mtime=mtime,
            permissions=permissions,
            version_id=version_id,
            corrected_mtime=corrected_mtime,
            incoming_vclock_raw=incoming_vclock_raw,
            local_bucket=local_bucket,
            file_location=file_location,
            file_location_tmp=file_location_tmp,
            backup_path=backup_path,
            sha256sum_tmp=sha256sum_tmp,
            bucket_root=bucket_root,
            dest_existed=dest_existed,
            dest_marker=dest_marker,
        )
        if early is not None:
            return early

        return await _broadcast_and_respond(
            src=src,
            cid=cid,
            local_bucket=local_bucket,
            bucket=bucket,
            total_bytes=total_bytes,
            t0=t0,
            resolution=resolution,
            result=result,
            path=path,
            file=file,
        )
    except OSError as e:
        log.error(f"Upload failed for file {file.filename}: {e}", exc_info=True)
        # Restore from backup if it exists
        if backup_path and Path(backup_path).exists() and file_location:
            replace(backup_path, file_location)
            log.info(f"Restored backup for file: {file_location}")
        raise UploadError(
            message=f"Upload failed for file {file.filename}",
            # Only expose the exception type, never the raw message
            # (which commonly embeds absolute filesystem paths). The full
            # traceback is already in the server log above.
            details={"filename": file.filename, "error": type(e).__name__},
        )
    finally:
        # Temp file that was never atomically renamed to its final location
        # (only present on failure paths; success/partial-success set it to
        # None before returning).
        if (
            file_location_tmp
            and Path(file_location_tmp).exists()
            and not is_partial_file(file_location_tmp)
        ):
            remove(file_location_tmp)
        # On failure, also clean up partial and backup artifacts so they
        # don't accumulate on disk.  On success/partial-success these are
        # intentionally preserved (partial files for resume, backup already
        # removed in the happy path).
        if file_location_tmp is not None:
            for f in (partial_file_path, backup_path):
                if f and Path(f).exists():
                    remove(f)
                    if is_partial_file(f):
                        log.debug(f"Cleaned up stale partial file: {f}")
