# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
File upload endpoint for HSYNC Server.

This module provides the REST API endpoint for file upload with:
- Resumable upload support
- MD5 checksum verification
- Timestamp preservation
- Permission setting
- Compression support
"""

from os import makedirs, remove
from os.path import exists, getsize
from shutil import move

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
    compute_md5,
    is_valid_bucket,
    is_valid_dir,
    set_permissions,
)
from hsync_server.compression import decompress_data
from hsync_server.config import HSYNC_BUCKETS, HSYNC_MAX_UPLOAD_SIZE
from hsync_server.exceptions import (
    ChecksumMismatchError,
    FileTooLargeError,
    InvalidBucketNameError,
    InvalidFilenameError,
    InvalidPathError,
    InvalidPermissionFormatError,
    UploadError,
)
from hsync_server.hashes import get_md5_hash, set_md5_hash
from hsync_server.models.event import Event, EventName
from hsync_server.models.file import File as LocalFile
from hsync_server.path_validation import is_valid_filename
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
    md5sum: str = Form(""),
    permissions: str = Form("-rw-rw-r--"),
    offset: int = Form(0),
    size: int = Form(0),
    compressed: str = Form("false"),
    compressed_size: int = Form(0),  # Reserved for future use
) -> JSONResponse:
    """
    Upload a file to a bucket with resumable upload support.

    Handles file upload with integrity verification (MD5), timestamp
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
        md5sum: Expected MD5 checksum for integrity verification.
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
        ChecksumMismatchError: If MD5 checksum verification fails.
        UploadError: If upload operation fails.
    """
    file_location_tmp = None
    partial_file_path = None
    is_resumable = offset > 0
    is_compressed = compressed.lower() == "true"
    try:
        # Validate inputs
        if not is_valid_bucket(bucket):
            raise InvalidBucketNameError(details={"bucket": bucket})
        if not is_valid_dir(path):
            raise InvalidPathError(details={"path": path})
        if permissions and not is_valid_permission_format(permissions):
            raise InvalidPermissionFormatError(details={"permissions": permissions})
        if not is_valid_filename(file.filename or ""):
            raise InvalidFilenameError(details={"filename": file.filename})

        # Define file paths
        file_directory = f"{HSYNC_BUCKETS}/{bucket}/{path}"
        file_location = f"{file_directory}/{file.filename}"
        partial_file_path = f"{file_location}.part"

        # Skip upload if identical file already exists (same MD5)
        if md5sum and exists(file_location) and md5sum == get_md5_hash(file_location):
            log.warning(f"File already present: {file_location}")
            # Update timestamps even if skipping upload
            update_file_timestamp(file_location, ctime, mtime)
            # Clean up partial file if exists
            if exists(partial_file_path):
                remove(partial_file_path)
            return JSONResponse(
                status_code=409,
                content={
                    "info": "File already present",
                    "file": file_location,
                },
            )

        # Ensure directory exists
        makedirs(file_directory, exist_ok=True)

        # Handle resumable upload
        if is_resumable:
            # Validate partial file exists and has expected size
            if not exists(partial_file_path):
                raise UploadError(
                    message="Cannot resume: partial file not found",
                    details={"offset": offset, "partial_file": partial_file_path},
                )
            current_size = getsize(partial_file_path)
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
                    if HSYNC_MAX_UPLOAD_SIZE > 0 and total_bytes > HSYNC_MAX_UPLOAD_SIZE:
                        raise FileTooLargeError(
                            message=f"File exceeds maximum size of {HSYNC_MAX_UPLOAD_SIZE} bytes",
                            details={
                                "max_size": HSYNC_MAX_UPLOAD_SIZE,
                                "received_bytes": total_bytes,
                            },
                        )
                    await f.write(chunk)

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
            if size > 0 and exists(partial_file_path):
                # Remove stale partial file
                remove(partial_file_path)

            async with NamedTemporaryFile(delete=False) as temp_file:
                file_location_tmp = str(temp_file.name)
                while chunk := await file.read(8192):
                    total_bytes += len(chunk)
                    if HSYNC_MAX_UPLOAD_SIZE > 0 and total_bytes > HSYNC_MAX_UPLOAD_SIZE:
                        raise FileTooLargeError(
                            message=f"File exceeds maximum size of {HSYNC_MAX_UPLOAD_SIZE} bytes",
                            details={
                                "max_size": HSYNC_MAX_UPLOAD_SIZE,
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

        # Verify MD5 checksum if provided
        md5sum_tmp = await compute_md5(file_location_tmp)
        if md5sum and md5sum != md5sum_tmp:
            raise ChecksumMismatchError(
                message=f"Checksum mismatch: expected {md5sum}, got {md5sum_tmp}",
                details={"expected": md5sum, "actual": md5sum_tmp},
            )

        # Store MD5 hash for future duplicate detection
        set_md5_hash(file_location, md5sum_tmp)

        # Atomically move file to final destination
        move(file_location_tmp, file_location)
        file_location_tmp = None  # Don't clean up - it's been moved

        # Broadcast "added" event to other connected clients
        src = LocalFile().get(file_location)
        local_bucket = f"{HSYNC_BUCKETS}/{bucket}"
        if src:
            manager = bucket_manager.get_manager(bucket)
            await manager.send_message(Event(cid, local_bucket, bucket, EventName.added, src))

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
        raise UploadError(
            message=f"Upload failed for file {file.filename}",
            details={"filename": file.filename, "error": str(e)},
        )
    finally:
        # Clean up temporary file if it exists (but not partial files)
        if (
            file_location_tmp
            and exists(file_location_tmp)
            and not file_location_tmp.endswith(".part")
        ):
            remove(file_location_tmp)
