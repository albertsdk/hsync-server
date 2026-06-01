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

from os import makedirs, remove
from pathlib import Path
from shutil import copy2, move

from aiofiles.tempfile import NamedTemporaryFile
from fastapi import File, Form, Request, UploadFile
from fastapi.responses import JSONResponse
from loguru import logger as log

from hsync_server.api.connection_manager import bucket_manager
from hsync_server.api.file_utils import (
    is_valid_permission_format,
    update_file_timestamp,
)
from hsync_server.api.utils import (
    compute_sha256,
    is_valid_bucket,
    is_valid_dir,
    set_permissions,
)
from hsync_server.compression import decompress_data
from hsync_server.config import get_buckets_path, get_max_upload_size, get_settings
from hsync_server.exceptions import (
    ChecksumMismatchError,
    FileTooLargeError,
    InsufficientStorageError,
    InvalidBucketNameError,
    InvalidFilenameError,
    InvalidPathError,
    InvalidPermissionFormatError,
    UploadError,
)
from hsync_server.hashes import get_sha256_hash, set_sha256_hash
from hsync_server.models.event import Event, EventName
from hsync_server.models.file import File as LocalFile
from hsync_server.models.file import invalidate_sha256_cache
from hsync_server.path_validation import is_valid_filename, sanitize_filename
from hsync_server.rate_limiter import RATE_LIMIT_UPLOAD, limiter
from hsync_server.routers import auth_router


@auth_router.post("/api/v1/buckets/{bucket}/files")
@limiter.limit(RATE_LIMIT_UPLOAD)
async def upload_file(
    request: Request,  # Required by rate limiter
    bucket: str,
    cid: str = Form(),
    file: UploadFile = File(...),
    path: str = Form(""),
    ctime: str = Form(""),
    mtime: str = Form(""),
    sha256sum: str = Form(""),
    permissions: str = Form("-rw-rw-r--"),
    offset: int = Form(0),
    size: int = Form(0),
    compressed: str = Form("false"),
    compressed_size: int = Form(0),  # Reserved for future use
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
        ctime: Creation time (Unix timestamp or ISO string).
        mtime: Modification time (Unix timestamp or ISO string).
        sha256sum: Expected SHA-256 checksum for integrity verification.
        permissions: File permissions in symbolic format.
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
    file_location_tmp = None
    partial_file_path = None
    backup_path = None
    file_location = None
    is_resumable = offset > 0
    is_compressed = compressed.lower() == "true"
    upload_successful = False
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

        # Check disk space before accepting upload
        min_free = get_settings().min_free_disk_space
        if min_free > 0:
            from shutil import disk_usage

            try:
                usage = disk_usage(buckets_path)
                if usage.free < min_free:
                    free_mb = usage.free / (1024 * 1024)
                    min_free_mb = min_free / (1024 * 1024)
                    raise InsufficientStorageError(
                        message=(
                            f"Insufficient storage: {free_mb:.0f} MB available, "
                            f"{min_free_mb:.0f} MB required"
                        ),
                        details={
                            "available_bytes": usage.free,
                            "required_bytes": min_free,
                        },
                    )
            except OSError:
                log.warning("Could not check disk space")

        # Validate and sanitize inputs
        if not is_valid_bucket(bucket):
            raise InvalidBucketNameError(details={"bucket": bucket})
        if not is_valid_dir(path):
            raise InvalidPathError(details={"path": path})
        if permissions and not is_valid_permission_format(permissions):
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

        # Define file paths
        file_directory = f"{buckets_path}/{bucket}/{path}"
        file_location = f"{file_directory}/{file.filename}"
        partial_file_path = f"{file_location}.part"

        # Skip upload if identical file already exists (same SHA-256)
        if (
            sha256sum
            and Path(file_location).exists()
            and sha256sum == get_sha256_hash(file_location)
        ):
            log.warning(f"File already present: {file_location}")
            # Update timestamps even if skipping upload
            update_file_timestamp(file_location, ctime, mtime)
            # Clean up partial file if exists
            if Path(partial_file_path).exists():
                remove(partial_file_path)
            upload_successful = True
            return JSONResponse(
                status_code=409,
                content={
                    "info": "File already present",
                    "file": file_location,
                },
            )

        # Ensure directory exists
        makedirs(file_directory, exist_ok=True)

        # Create backup of existing file before overwriting
        # This ensures readers can still access the complete original file
        # while the new version is being uploaded
        backup_path = f"{file_location}.backup"
        if Path(file_location).exists():
            copy2(file_location, backup_path)

        # Handle resumable upload
        if is_resumable:
            # Validate partial file exists and has expected size
            if not Path(partial_file_path).exists():
                raise UploadError(
                    message="Cannot resume: partial file not found",
                    details={"offset": offset, "partial_file": partial_file_path},
                )
            current_size = Path(partial_file_path).stat().st_size
            if current_size != offset:
                raise UploadError(
                    message=f"Offset mismatch: expected {current_size}, got {offset}",
                    details={"expected_offset": current_size, "provided_offset": offset},
                )

            # Append to partial file
            total_bytes = offset
            import aiofiles

            async with aiofiles.open(partial_file_path, "ab") as f:
                while chunk := await file.read(8192):
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

            # Check if upload is complete
            if size > 0 and total_bytes < size:
                # Partial upload - more chunks expected
                upload_successful = True
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
            # move() below resolves to an atomic same-filesystem rename instead
            # of a non-atomic copy+remove (which could leave a torn file).
            async with NamedTemporaryFile(delete=False, dir=file_directory) as temp_file:
                file_location_tmp = str(temp_file.name)
                while chunk := await file.read(8192):
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

            # Handle compressed upload - decompress the file
            if is_compressed and total_bytes > 0:
                import aiofiles

                async with aiofiles.open(file_location_tmp, "rb") as f:
                    compressed_data = await f.read()
                log.debug(f"Decompressing uploaded file: {total_bytes} bytes -> ~{size} bytes")
                decompressed_data = decompress_data(compressed_data)
                async with aiofiles.open(file_location_tmp, "wb") as f:
                    await f.write(decompressed_data)
                total_bytes = len(decompressed_data)

            # Check if this is partial upload (size provided but not fully received)
            if size > 0 and total_bytes < size and not is_compressed:
                # Move temp file to partial file location
                move(file_location_tmp, partial_file_path)
                file_location_tmp = None
                upload_successful = True
                return JSONResponse(
                    status_code=202,
                    content={
                        "info": "Partial upload received",
                        "offset": total_bytes,
                        "remaining": size - total_bytes,
                    },
                )

        # Set timestamps on file
        update_file_timestamp(file_location_tmp, ctime, mtime)

        # Set permissions on file
        if permissions:
            await set_permissions(file_location_tmp, permissions)

        # Verify SHA-256 checksum if provided
        sha256sum_tmp = await compute_sha256(file_location_tmp)
        if sha256sum and sha256sum != sha256sum_tmp:
            raise ChecksumMismatchError(
                message=f"Checksum mismatch: expected {sha256sum}, got {sha256sum_tmp}",
                details={"expected": sha256sum, "actual": sha256sum_tmp},
            )

        # Store SHA-256 hash for future duplicate detection
        set_sha256_hash(file_location, sha256sum_tmp)

        # Atomically move file to final destination
        move(file_location_tmp, file_location)
        file_location_tmp = None  # Don't clean up - it's been moved

        # Invalidate SHA-256 cache for this file (content has changed)
        invalidate_sha256_cache(file_location)

        # Clean up backup file after successful upload
        if backup_path and Path(backup_path).exists():
            remove(backup_path)

        # Broadcast "added" event to other connected clients
        src = LocalFile().get(file_location)
        local_bucket = f"{buckets_path}/{bucket}"
        if src:
            manager = bucket_manager.get_manager(bucket)
            await manager.send_message(Event(cid, local_bucket, bucket, EventName.added, src))

        upload_successful = True
        return JSONResponse(
            status_code=201,
            content={
                "info": "File uploaded successfully",
                "file": f"{bucket}/{path}/{file.filename}",
            },
        )
    except (
        InvalidBucketNameError,
        InvalidPathError,
        InvalidPermissionFormatError,
        ChecksumMismatchError,
        FileTooLargeError,
    ):
        # Re-raise custom exceptions - they'll be handled by global handler
        raise
    except Exception as e:
        log.error(f"Upload failed for file {file.filename}: {e}", exc_info=True)
        # Restore from backup if it exists
        if backup_path and Path(backup_path).exists() and file_location:
            move(backup_path, file_location)
            log.info(f"Restored backup for file: {file_location}")
        raise UploadError(
            message=f"Upload failed for file {file.filename}",
            details={"filename": file.filename, "error": str(e)},
        )
    finally:
        if upload_successful:
            # Success: clean up temp file, but preserve partial files
            # for potential resumable upload continuation
            if (
                file_location_tmp
                and Path(file_location_tmp).exists()
                and not file_location_tmp.endswith(".part")
            ):
                remove(file_location_tmp)
        else:
            # Failure: clean up ALL leftover temp files, including .part files,
            # to prevent accumulation of stale partial uploads on disk
            for f in (file_location_tmp, partial_file_path, backup_path):
                if f and Path(f).exists():
                    remove(f)
                    if f.endswith(".part"):
                        log.debug(f"Cleaned up stale partial file: {f}")
