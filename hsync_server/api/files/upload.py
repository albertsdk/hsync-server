# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

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
import json
from os import fsync, remove, replace
from pathlib import Path
from shutil import copy2
from time import monotonic
from typing import Annotated

import aiofiles
import zstandard as zstd
from aiofiles.tempfile import NamedTemporaryFile
from fastapi import Depends, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse
from hsync_common.clock import clamp_incoming_corrected_mtime
from hsync_common.constants import PARTIAL_DOWNLOAD_SUFFIX
from hsync_common.format import format_bytes, format_duration
from hsync_common.hashing import compute_sha256_async, is_sha256_hex
from hsync_common.models.event import Event, EventName, is_valid_cid
from hsync_common.models.file import File as LocalFile
from hsync_common.models.file import invalidate_sha256_cache, is_hidden_file, is_partial_file
from hsync_common.models.version import relpath_in_bucket
from hsync_common.path_validation import is_internal_name, is_valid_filename, sanitize_filename
from hsync_common.permissions import is_valid_octal_permissions
from hsync_common.time_utils import conflict_timestamp
from loguru import logger as log

from hsync_server.api.authz import ensure_bucket_access
from hsync_server.api.connection_manager import bucket_manager
from hsync_server.api.file_utils import (
    update_file_timestamp,
)
from hsync_server.api.utils import (
    is_valid_bucket,
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
from hsync_server.config import get_buckets_path, get_max_upload_size, get_settings
from hsync_server.constants import BACKUP_SUFFIX, DEFAULT_FILE_PERMISSIONS, ActionLabel
from hsync_server.exceptions import (
    ChecksumMismatchError,
    ConflictLostError,
    FileTooLargeError,
    InsufficientStorageError,
    InvalidBucketNameError,
    InvalidFilenameError,
    InvalidPathError,
    InvalidPermissionFormatError,
    SupersededError,
    UploadError,
    ValidationError,
)
from hsync_server.file_lock_manager import get_file_lock_manager
from hsync_server.hashes import get_sha256_hash, set_sha256_hash
from hsync_server.rate_limiter import RATE_LIMIT_UPLOAD, limiter
from hsync_server.routers import auth_router
from hsync_server.tombstones import clear_deletion
from hsync_server.versions import record_version, server_version_info

from .permissions import apply_permissions_to_existing_file

# Chunk size for copying the spooled upload body to its destination file.
# 8 KiB made the copy loop event-loop-bound on large files (one await per
# 8 KiB); 256 KiB matches the transfer chunk tiers used across the ecosystem.
_UPLOAD_COPY_CHUNK = 256 * 1024


async def _flush_to_disk(handle) -> None:
    """Flush an open aiofiles handle's buffers to physical storage (fsync)."""
    await handle.flush()
    await asyncio.to_thread(fsync, handle.fileno())


@auth_router.post("/api/v1/buckets/{bucket}/files")
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

        # Early validation: Check size BEFORE accepting any data
        # This prevents DoS attacks with huge files
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
        min_free = get_settings().min_free_disk_space
        if min_free > 0:
            from shutil import disk_usage

            try:
                usage = disk_usage(buckets_path)
                required = min_free + size
                if usage.free < required:
                    free_mb = usage.free / (1024 * 1024)
                    required_mb = required / (1024 * 1024)
                    raise InsufficientStorageError(
                        message=(
                            f"Insufficient storage: {free_mb:.0f} MB available, "
                            f"{required_mb:.0f} MB required"
                        ),
                        details={
                            "available_bytes": usage.free,
                            "required_bytes": required,
                        },
                    )
            except OSError:
                log.warning("Could not check disk space")

        # Validate and sanitize inputs
        if not is_valid_bucket(bucket):
            raise InvalidBucketNameError(details={"bucket": bucket})
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
                    "Compressed uploads are not resumable: "
                    "send the full payload in a single request"
                ),
                details={"offset": offset, "compressed": compressed},
            )
        if permissions and not is_valid_octal_permissions(permissions):
            raise InvalidPermissionFormatError(details={"permissions": permissions})

        # Sanitize filename to handle dangerous characters automatically
        original_filename = file.filename or ""
        safe_filename = sanitize_filename(original_filename)

        # If sanitization changed the filename, warn but allow it
        if safe_filename != original_filename:
            log.warning(f"Filename sanitized: '{original_filename}' -> '{safe_filename}'")
            # Update file.filename with sanitized name for further processing
            file.filename = safe_filename

        if not is_valid_filename(file.filename or ""):
            raise InvalidFilenameError(details={"filename": file.filename})

        # Internal sync-engine artifacts must never enter the storage via the
        # wire: a peer that could (re)write them would alter every other
        # peer's sync behaviour through the sync channel itself.
        if is_internal_name(file.filename or ""):
            raise InvalidFilenameError(
                message="Internal sync-engine filenames are not allowed",
                details={"filename": file.filename},
            )

        # Confine the upload directory to *this* bucket: the raw form path is
        # untrusted input and must never escape into a sibling bucket.
        file_directory = resolve_within_bucket(bucket, path)
        if file_directory is None:
            raise InvalidPathError(details={"path": path})

        # Define file paths
        file_location = f"{file_directory}/{file.filename}"
        partial_file_path = f"{file_location}{PARTIAL_DOWNLOAD_SUFFIX}"

        # Skip upload if identical file already exists (same SHA-256). A
        # permission-only change (chmod) reuses this branch: the content is
        # already on disk, so instead of rejecting the upload we apply the
        # new permissions and notify the other connected clients — the
        # permission change is a normal causal `modified` event.
        if (
            sha256sum
            and Path(file_location).exists()
            and hmac.compare_digest(sha256sum, get_sha256_hash(file_location) or "")
        ):
            log.warning(f"File already present: {file_location}")
            # Update timestamps even if skipping upload
            update_file_timestamp(file_location, ctime, mtime)
            # The utime pass changed the (mtime_ns) the cache keyed on: refresh
            # the entry so the next identical upload still hits the dup-skip.
            set_sha256_hash(file_location, sha256sum)
            # The file exists: drop any stale deletion marker for it.
            clear_deletion(bucket, f"{buckets_path}/{bucket}", file_location)
            # Clean up partial file if exists
            if Path(partial_file_path).exists():
                remove(partial_file_path)
            # Shared permission-only branch (same logic as the dedicated
            # PATCH /files/permissions endpoint): applies the new mode when it
            # diverges and re-broadcasts a causal `modified` event.
            permissions_only = await apply_permissions_to_existing_file(
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
                        "file": f"{bucket}/{path}/{file.filename}"
                        if path
                        else f"{bucket}/{file.filename}",
                    },
                )
            return JSONResponse(
                status_code=409,
                content={
                    "info": "File already present",
                    "file": f"{bucket}/{path}/{file.filename}"
                    if path
                    else f"{bucket}/{file.filename}",
                },
            )

        # Ensure directory exists
        Path(file_directory).mkdir(parents=True, exist_ok=True)

        # Create backup of existing file before overwriting
        # This ensures readers can still access the complete original file
        # while the new version is being uploaded
        backup_path = f"{file_location}{BACKUP_SUFFIX}"
        if Path(file_location).exists():
            copy2(file_location, backup_path)

        # Fused checksum: updated while the body is copied so a complete
        # upload needs no second full read of the temp file (the previous
        # implementation re-read the whole file with compute_sha256_async
        # after the copy). Excluded for compressed uploads — the verified
        # hash is of the DECOMPRESSED content, which only exists after the
        # raw copy is expanded below.
        fused_sha = None if is_compressed else hashlib.sha256()

        # Handle resumable upload
        if is_resumable:
            # Validate partial file exists and has expected size
            # Client-input errors (missing file, wrong offset) are 400,
            # not 500 — the client must correct its request.
            if not Path(partial_file_path).exists():
                raise ValidationError(
                    message="Cannot resume: partial file not found",
                    details={"offset": offset},
                )
            current_size = Path(partial_file_path).stat().st_size
            if current_size != offset:
                raise ValidationError(
                    message=f"Offset mismatch: expected {current_size}, got {offset}",
                    details={"expected_offset": current_size, "provided_offset": offset},
                )

            # Append to partial file
            total_bytes = offset
            # Fused checksum (resume): ingest the already-received prefix so
            # the digest covers the complete file. Compressed uploads are
            # never resumable (rejected above), so fused_sha is set here.
            assert fused_sha is not None
            async with aiofiles.open(partial_file_path, "rb") as prefix:
                while prefix_chunk := await prefix.read(_UPLOAD_COPY_CHUNK):
                    fused_sha.update(prefix_chunk)
            async with aiofiles.open(partial_file_path, "ab") as f:
                while chunk := await file.read(_UPLOAD_COPY_CHUNK):
                    total_bytes += len(chunk)
                    if max_upload_size > 0 and total_bytes > max_upload_size:
                        raise FileTooLargeError(
                            message=f"File exceeds maximum size of {max_upload_size} bytes",
                            details={
                                "max_size": max_upload_size,
                                "received_bytes": total_bytes,
                            },
                        )
                    await f.write(chunk)
                    fused_sha.update(chunk)
                # Ensure appended data reaches physical storage
                await _flush_to_disk(f)

            # Check if upload is complete
            if size > 0 and total_bytes < size:
                # Partial upload - more chunks expected
                return JSONResponse(
                    status_code=202,
                    content={
                        "info": "Partial upload received",
                        "offset": total_bytes,
                        "remaining": size - total_bytes,
                    },
                )

            # Upload complete - verify and finalize
            file_location_tmp = partial_file_path
        else:
            # Standard upload to temp file (new upload or first chunk)
            total_bytes = 0

            # If size > 0, this might be first chunk of a multi-chunk upload
            if size > 0 and Path(partial_file_path).exists():
                # Remove stale partial file
                remove(partial_file_path)

            # Create the temp file in the destination directory so the final
            # os.replace() below resolves to an atomic same-filesystem rename
            # instead of a non-atomic copy+remove (which could leave a torn file).
            # The dot prefix keeps the in-flight artifact hidden: it must not
            # appear in /files listings (peers would sync torn content).
            async with NamedTemporaryFile(
                delete=False, dir=file_directory, prefix=".hsync-upload-"
            ) as temp_file:
                file_location_tmp = str(temp_file.name)
                while chunk := await file.read(_UPLOAD_COPY_CHUNK):
                    total_bytes += len(chunk)
                    if max_upload_size > 0 and total_bytes > max_upload_size:
                        raise FileTooLargeError(
                            message=f"File exceeds maximum size of {max_upload_size} bytes",
                            details={
                                "max_size": max_upload_size,
                                "received_bytes": total_bytes,
                            },
                        )
                    await temp_file.write(chunk)
                    if fused_sha is not None:
                        fused_sha.update(chunk)
                # Ensure written data reaches physical storage
                await _flush_to_disk(temp_file)

            # Handle compressed upload - decompress the file
            if is_compressed and total_bytes > 0:
                # Anti decompression-bomb: the compressed payload may expand
                # arbitrarily, so the decompressed size is hard-capped. The
                # cap is the declared file size when known (the content must
                # match it exactly — integrity is verified below), otherwise
                # the configured upload limit.
                if size > 0:
                    max_decompressed = size
                elif max_upload_size > 0:
                    max_decompressed = max_upload_size
                else:
                    raise ValidationError(
                        message="Missing/invalid 'size' for compressed upload",
                        details={"size": size},
                    )
                # Streaming decompression: the compressed temp file is expanded
                # into a second temp file chunk-by-chunk, so neither the
                # compressed input nor the expanded output (each up to the cap)
                # is ever fully buffered in memory. The expand runs in a
                # worker thread (blocking file I/O + CPU-bound decompression
                # must not stall the event loop).
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
                            details={"size": size, "compressed_bytes": total_bytes},
                        ) from exc
                    if size > 0 and decompressed_bytes != size:
                        raise ValidationError(
                            message=(
                                f"Decompressed size mismatch: expected {size}, "
                                f"got {decompressed_bytes}"
                            ),
                            details={"expected": size, "actual": decompressed_bytes},
                        )
                    # Swap the expanded content back onto the upload temp path
                    # (same-filesystem rename).
                    replace(decompressed_tmp, file_location_tmp)
                    total_bytes = decompressed_bytes
                finally:
                    if Path(decompressed_tmp).exists():
                        remove(decompressed_tmp)

            # Check if this is partial upload (size provided but not fully received)
            if size > 0 and total_bytes < size and not is_compressed:
                # Move temp file to partial file location (same-filesystem rename)
                replace(file_location_tmp, partial_file_path)
                file_location_tmp = None
                return JSONResponse(
                    status_code=202,
                    content={
                        "info": "Partial upload received",
                        "offset": total_bytes,
                        "remaining": size - total_bytes,
                    },
                )

        # SHA-256 integrity is mandatory: the received bytes must hash to the
        # declared checksum, otherwise the upload is rejected (400).  Verify
        # *before* touching any file metadata so rejected uploads are as
        # cheap as possible and never alter existing on-disk state.
        if fused_sha is not None:
            # Uncompressed uploads: the digest was fused into the copy loop
            # (no second full read of the temp file).
            sha256sum_tmp = fused_sha.hexdigest()
        else:
            # Compressed uploads: the fused hasher would cover the raw
            # compressed bytes — hash the finalized decompressed file.
            sha256sum_tmp = await compute_sha256_async(file_location_tmp)
        if not hmac.compare_digest(sha256sum, sha256sum_tmp):
            raise ChecksumMismatchError(
                message=f"Checksum mismatch: expected {sha256sum}, got {sha256sum_tmp}",
                details={"expected": sha256sum, "actual": sha256sum_tmp},
            )

        # Parse the incoming causal vector clock (JSON form field; malformed ⇒
        # empty clock, handled natively by the arbitration matrix).
        try:
            incoming_vclock_raw = json.loads(vclock) if vclock else None
        except (ValueError, TypeError):
            incoming_vclock_raw = None

        local_bucket = f"{buckets_path}/{bucket}"
        # The per-path lock serializes the arbitration critical section: read
        # store → decide → write file → record. Two concurrent uploads for the
        # same path are ordered, so the second sees the version the first
        # registered (ADR-0022 §3.4.6).
        lock_manager = get_file_lock_manager()
        async with lock_manager.acquire(file_location):
            relpath = relpath_in_bucket(file_location, local_bucket) or ""
            disk_hash = get_sha256_hash(file_location) if Path(file_location).exists() else None

            # ADR-0023 receiver-side guard: a far-future peer value cannot win
            # every tie-break — it is clamped against the receiver's corrected
            # clock (conflict engineering).
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

            if result.outcome is ArbitrationOutcome.superseded:
                # Stale upload (causally dominated): discard the temp file,
                # no conflicted copy (the ancestor's data is in the winner).
                remove(file_location_tmp)
                file_location_tmp = None
                if backup_path and Path(backup_path).exists():
                    remove(backup_path)
                raise SupersededError(
                    message="Upload superseded by a newer version on the server",
                    details={"sha256sum": sha256sum_tmp[:12]},
                )

            if result.outcome is ArbitrationOutcome.conflict_lost:
                # Incoming loses the tie-break: preserve its bytes as a
                # conflicted copy, keep the winner on disk, reject the upload.
                ts = conflict_timestamp()
                conflicted = conflicted_copy_name(file_location, result.loser_cid, ts)
                replace(file_location_tmp, conflicted)
                file_location_tmp = None
                if backup_path and Path(backup_path).exists():
                    remove(backup_path)
                raise ConflictLostError(
                    message="Upload lost a conflict tie-break; content preserved as conflicted copy",
                    details={
                        "sha256sum": sha256sum_tmp[:12],
                        "conflicted_copy": Path(conflicted).name,
                    },
                )

            # accepted or accepted_conflict_won: write the incoming content.
            # On accepted_conflict_won, preserve the previous (losing) content
            # as a conflicted copy using the backup created earlier.
            resolution = result.outcome.value
            if (
                result.outcome is ArbitrationOutcome.accepted_conflict_won
                and backup_path
                and Path(backup_path).exists()
            ):
                ts = conflict_timestamp()
                conflicted = conflicted_copy_name(file_location, result.loser_cid, ts)
                replace(backup_path, conflicted)
                _prev_sha = (disk_hash or "")[:12]
                _new_sha = sha256sum_tmp[:12]
                log.warning(
                    f'{ActionLabel.UPLOAD} Conflict detected on "{bucket}/{path}/{file.filename}" '
                    f"(prev SHA {_prev_sha}…, new SHA {_new_sha}…): "
                    f'previous version preserved as "{Path(conflicted).name}"'
                )
            elif backup_path and Path(backup_path).exists():
                remove(backup_path)

            # Atomically move file to final destination (same-filesystem rename)
            replace(file_location_tmp, file_location)
            file_location_tmp = None  # Don't clean up - it's been moved

            # Set timestamps and permissions on the *final* file after the
            # atomic rename — metadata is only applied to content that passed
            # integrity verification.
            update_file_timestamp(file_location, ctime, mtime)

            if permissions:
                await set_permissions(file_location, permissions)

            # Invalidate SHA-256 cache for this file (content has changed)
            invalidate_sha256_cache(file_location)

            # Store SHA-256 hash for future duplicate detection LAST, after
            # the timestamp/permission pass: the cache captures the
            # (mtime_ns, size) of the *published* file, so it must be written
            # once the final path exists AND its metadata is final — a set
            # before the rename would be a no-op (file missing) and a set
            # before the utime/chmod pass would store already-stale metadata
            # (the freshness check would drop the entry on the first read).
            set_sha256_hash(file_location, sha256sum_tmp)

            # A successful upload (re-)creates the file: drop any deletion marker
            # so peers do not delete it on their next full sync.
            clear_deletion(bucket, local_bucket, file_location)

            # Record the agreed version (merge of incoming + stored clocks, D11,
            # plus the ADR-0023 corrected mtime).
            record_version(
                bucket,
                local_bucket,
                file_location,
                base_hash=sha256sum_tmp,
                vclock=result.record_vclock,
                version_id=version_id or None,
                origin_cid=cid if is_valid_cid(cid) else None,
                corrected_mtime=result.record_corrected_mtime,
            )

            # Capture the broadcast metadata while STILL holding the per-path
            # lock: it must describe exactly the generation this upload just
            # published — an unlocked read could interleave with a later
            # same-path upload and broadcast its content/version under this
            # upload's CID. The hash-on-miss inside File.get is blocking I/O,
            # so it runs on a worker thread; the lock is already held across
            # awaited disk I/O above (set_permissions), so this adds no new
            # lock-hold category. Hidden files are excluded (as from the
            # /files listing): peers syncing hidden files pick them up via
            # their own watcher or the next full reconciliation.
            if is_hidden_file(file_location):
                src = None
            else:
                src = await asyncio.to_thread(LocalFile.get, file_location)
                if src is not None:
                    src.version = server_version_info(bucket, local_bucket, file_location)

        # Broadcast the pre-captured metadata outside the lock.
        if src:
            manager = bucket_manager.get_manager(bucket)
            await manager.send_message(Event(cid, local_bucket, bucket, EventName.added, src))

        log.info(
            f'{ActionLabel.UPLOAD} "{bucket}/{path}/{file.filename}" (CID: {cid}) '
            f"{format_bytes(total_bytes)} "
            f"{format_duration(monotonic() - t0, subsecond=True)}"
        )
        return JSONResponse(
            status_code=201,
            content={
                "info": "File uploaded successfully",
                "file": f"{bucket}/{path}/{file.filename}",
                "resolution": resolution,
                # ADR-0023: the recorded causal version (merged clock + corrected
                # mtime) so the uploader can mirror the agreed base exactly.
                "version": {
                    "vclock": result.record_vclock,
                    "corrected_mtime": result.record_corrected_mtime,
                },
            },
        )
    except (
        InvalidBucketNameError,
        InvalidPathError,
        InvalidFilenameError,
        InvalidPermissionFormatError,
        ChecksumMismatchError,
        FileTooLargeError,
        InsufficientStorageError,
        SupersededError,
        ConflictLostError,
        ValidationError,
    ):
        # Re-raise custom exceptions - they'll be handled by global handler
        raise
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
