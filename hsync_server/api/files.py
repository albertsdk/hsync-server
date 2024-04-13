# HSYNC Server - HTTP-based file synchronization server
# Copyright (C) 2025 Alberto Basso
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

"""
File operations API for HSYNC Server.

This module provides REST API endpoints for file management:
- File upload with integrity verification (MD5)
- File download with streaming response
- File listing with sorting options
- File deletion with event broadcasting

All endpoints require API key authentication via Bearer token.
Rate limiting is applied to protect against abuse.
"""

import logging

from aiofiles.ospath import isfile
from aiofiles.tempfile import NamedTemporaryFile
from fastapi import File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from os import makedirs, remove, walk
from os.path import dirname, exists, getsize
from shutil import move
from typing import Optional

from hsync_server.api.connection_manager import bucket_manager
from hsync_server.api.file_utils import is_valid_permission_format, update_file_timestamp
from hsync_server.api.utils import compute_md5, is_valid_bucket, is_valid_dir, set_permissions
from hsync_server.compression import (
    compress_data,
    decompress_data,
    should_compress,
    CONTENT_ENCODING_ZSTD,
)
from hsync_server.config import HSYNC_BUCKETS, HSYNC_MAX_UPLOAD_SIZE, get_compression_enabled
from hsync_server.models.dir import is_hidden_dir
from hsync_server.models.file import is_hidden_file
from hsync_server.exceptions import (
    BucketNotFoundError,
    ChecksumMismatchError,
    DeleteError,
    FileNotFoundError as HSyncFileNotFoundError,
    FileTooLargeError,
    InvalidBucketNameError,
    InvalidFilenameError,
    InvalidPathError,
    InvalidPermissionFormatError,
    InvalidSyncOrderError,
    UploadError,
)
from hsync_server.path_validation import is_valid_filename
from hsync_server.hashes import get_md5_hash, set_md5_hash
from hsync_server.models.event import Event, EventName
from hsync_server.models.file import File as LocalFile
from hsync_server.rate_limiter import (
    limiter,
    RATE_LIMIT_DEFAULT,
    RATE_LIMIT_DOWNLOAD,
    RATE_LIMIT_UPLOAD,
)
from hsync_server.routers import auth_router


# Logger instance for this module (uses __name__ for module-level filtering)
LOG = logging.getLogger(__name__)


@auth_router.post("/api/v1/buckets/{bucket}/files")
@limiter.limit(RATE_LIMIT_UPLOAD)
async def upload_file(
    request: Request,
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
    compressed_size: int = Form(0),
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
            LOG.warning(f"File already present: {file_location}")
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
                            details={"max_size": HSYNC_MAX_UPLOAD_SIZE, "received_bytes": total_bytes},
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
                            details={"max_size": HSYNC_MAX_UPLOAD_SIZE, "received_bytes": total_bytes},
                        )
                    await temp_file.write(chunk)

            # Handle compressed upload - decompress the file
            if is_compressed and total_bytes > 0:
                import aiofiles
                async with aiofiles.open(file_location_tmp, "rb") as f:
                    compressed_data = await f.read()
                LOG.debug(
                    f"Decompressing uploaded file: {total_bytes} bytes -> ~{size} bytes"
                )
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
            await manager.send_message(
                Event(cid, local_bucket, bucket, EventName.added, src)
            )

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
        LOG.error(f"Upload failed for file {file.filename}: {e}", exc_info=True)
        raise UploadError(
            message=f"Upload failed for file {file.filename}",
            details={"filename": file.filename, "error": str(e)},
        )
    finally:
        # Clean up temporary file if it exists (but not partial files)
        if file_location_tmp and exists(file_location_tmp) and not file_location_tmp.endswith(".part"):
            remove(file_location_tmp)


@auth_router.get("/api/v1/buckets/{bucket}/files/download")
@limiter.limit(RATE_LIMIT_DOWNLOAD)
async def download_file(
    request: Request, bucket: str, filename: str
):
    """
    Download a file from a bucket with optional zstd compression.

    Streams the file content as a response with appropriate headers.
    If compression is enabled and beneficial, compresses the file before sending.

    Args:
        request: The incoming FastAPI request.
        bucket: Name of the bucket containing the file.
        filename: Path to the file within the bucket.

    Returns:
        FileResponse or Response with the file content for download.

    Raises:
        InvalidBucketNameError: If bucket name is invalid.
        InvalidPathError: If path is invalid.
        HSyncFileNotFoundError: If file does not exist.
    """
    from fastapi.responses import Response

    if not is_valid_bucket(bucket):
        raise InvalidBucketNameError(details={"bucket": bucket})
    if not is_valid_dir(filename):
        raise InvalidPathError(details={"path": filename})

    file = f"{HSYNC_BUCKETS}/{bucket}/{filename}"

    if not await isfile(file):
        raise HSyncFileNotFoundError(details={"bucket": bucket, "filename": filename})

    file_size = getsize(file)

    # Check if compression should be used:
    # - Compression is enabled on server
    # - File is compressible (not too small, not pre-compressed)
    # - No Range header (resume requests skip compression)
    has_range_header = "range" in request.headers
    use_compression = (
        get_compression_enabled()
        and should_compress(filename, file_size)
        and not has_range_header
    )

    if use_compression:
        # Read and compress file
        compressed_data = compress_data(open(file, "rb").read())
        compression_ratio = len(compressed_data) / file_size * 100
        LOG.debug(
            f"Compressed download: {file_size} -> {len(compressed_data)} bytes "
            f"({compression_ratio:.1f}%)"
        )
        return Response(
            content=compressed_data,
            media_type="application/octet-stream",
            headers={
                "Content-Encoding": CONTENT_ENCODING_ZSTD,
                "Content-Disposition": f'attachment; filename="{filename.split("/")[-1]}"',
            },
        )

    # Standard download (streaming, supports Range requests)
    return FileResponse(file, filename=filename)


@auth_router.get("/api/v1/buckets/{bucket}/files/upload-status")
@limiter.limit(RATE_LIMIT_DEFAULT)
async def get_upload_status(
    request: Request,
    bucket: str,
    filename: str,
    md5sum: str = "",
    size: int = 0,
) -> JSONResponse:
    """
    Get upload status for resumable uploads.

    Checks if a partial file exists from a previous interrupted upload
    and returns the current offset from which the client can resume.

    Args:
        request: The incoming FastAPI request.
        bucket: Target bucket name.
        filename: Path to the file within the bucket.
        md5sum: Expected MD5 checksum of the complete file.
        size: Expected size of the complete file.

    Returns:
        JSON response with upload status:
        - offset: Number of bytes already uploaded (0 if no partial file)
        - status: "resumable" if partial file exists, "new" otherwise

    Raises:
        InvalidBucketNameError: If bucket name is invalid.
        InvalidPathError: If path is invalid.
    """
    if not is_valid_bucket(bucket):
        raise InvalidBucketNameError(details={"bucket": bucket})
    if not is_valid_dir(dirname(filename)):
        raise InvalidPathError(details={"path": filename})

    file_path = f"{HSYNC_BUCKETS}/{bucket}/{filename}"
    partial_path = f"{file_path}.part"

    # Check if complete file already exists with same MD5
    if md5sum and exists(file_path) and md5sum == get_md5_hash(file_path):
        return JSONResponse(
            status_code=200,
            content={
                "status": "complete",
                "offset": 0,
                "message": "File already exists with same checksum",
            },
        )

    # Check for partial file
    if exists(partial_path):
        partial_size = getsize(partial_path)
        # Validate partial file is not larger than expected
        if size > 0 and partial_size >= size:
            # Partial file is invalid, remove it
            remove(partial_path)
            return JSONResponse(
                status_code=200,
                content={
                    "status": "new",
                    "offset": 0,
                    "message": "Previous partial file was invalid, starting fresh",
                },
            )
        return JSONResponse(
            status_code=200,
            content={
                "status": "resumable",
                "offset": partial_size,
                "message": f"Can resume from byte {partial_size}",
            },
        )

    return JSONResponse(
        status_code=200,
        content={
            "status": "new",
            "offset": 0,
            "message": "No partial file exists",
        },
    )


@auth_router.get("/api/v1/buckets/{bucket}/files")
@limiter.limit(RATE_LIMIT_DEFAULT)
async def get_files(
    request: Request,
    bucket: str,
    path: Optional[str] = None,
    sync_order: str = "mtime",
) -> JSONResponse:
    """
    List all files in a bucket or subdirectory.

    Recursively walks the directory tree and returns metadata for
    all non-hidden files, sorted by the specified timestamp field.

    Args:
        bucket: Name of the bucket to list files from.
        path: Optional subdirectory path within the bucket.
        sync_order: Sort order ('mtime' or 'ctime').

    Returns:
        JSON response with list of file metadata dictionaries.

    Raises:
        InvalidBucketNameError: If bucket name is invalid.
        InvalidPathError: If path is invalid.
        InvalidSyncOrderError: If sync_order is invalid.
        BucketNotFoundError: If bucket does not exist.
    """
    # Validate inputs
    if not is_valid_bucket(bucket):
        raise InvalidBucketNameError(details={"bucket": bucket})
    if path and not is_valid_dir(path):
        raise InvalidPathError(details={"path": path})

    valid_orders = ["mtime", "ctime"]
    if sync_order not in valid_orders:
        raise InvalidSyncOrderError(
            message=f"Invalid sync order '{sync_order}'. Must be one of {valid_orders}.",
            details={"sync_order": sync_order, "valid_orders": valid_orders},
        )

    # Construct directory path
    dir = f"{HSYNC_BUCKETS}/{bucket}/{path or ''}".rstrip("/")

    if not exists(dir):
        raise BucketNotFoundError(details={"bucket": bucket, "path": path})

    # Collect file metadata
    file_list = []
    for root, dirs, files in walk(dir):
        # Skip hidden directories using is_hidden_dir
        dirs[:] = [d for d in dirs if not is_hidden_dir(f"{root}/{d}")]

        for file in files:
            file_path = f"{root}/{file}"
            # Skip hidden files using is_hidden_file
            if is_hidden_file(file_path):
                continue

            # Get file info and normalize path relative to bucket
            file_obj = LocalFile().get(file, root)
            if file_obj:
                file_info = file_obj.to_dict()
                file_data = (
                    LocalFile()
                    .from_dict(file_info, f"{HSYNC_BUCKETS}/{bucket}")
                    .to_dict()
                )
                file_list.append(file_data)

    # Sort by specified timestamp field
    file_list.sort(key=lambda x: x.get(sync_order))

    return JSONResponse(
        status_code=200,
        content=file_list,
    )


@auth_router.delete("/api/v1/buckets/{bucket}/files")
async def delete_file(
    bucket: str,
    cid: str = Form(),
    filename: str = Form(),
) -> JSONResponse:
    """
    Delete a file from a bucket.

    Removes the file and broadcasts a "deleted" event to all
    connected WebSocket clients.

    Args:
        bucket: Name of the bucket containing the file.
        cid: Client ID for event broadcasting.
        filename: Path to the file within the bucket.

    Returns:
        JSON response confirming deletion.

    Raises:
        InvalidBucketNameError: If bucket name is invalid.
        BucketNotFoundError: If bucket does not exist.
        InvalidPathError: If path is invalid.
        HSyncFileNotFoundError: If file does not exist.
        DeleteError: If deletion fails.
    """
    # Validate bucket name
    if not is_valid_bucket(bucket):
        raise InvalidBucketNameError(details={"bucket": bucket})

    bucket_dir = f"{HSYNC_BUCKETS}/{bucket}"
    if not exists(bucket_dir):
        raise BucketNotFoundError(details={"bucket": bucket})

    # Validate path to prevent traversal attacks
    if not is_valid_dir(filename):
        raise InvalidPathError(details={"path": filename})

    file = f"{bucket_dir}/{filename}"

    if not exists(file):
        raise HSyncFileNotFoundError(details={"bucket": bucket, "filename": filename})

    try:
        # Broadcast "deleted" event to other clients before deletion
        src = LocalFile().get(file)
        local_bucket = f"{HSYNC_BUCKETS}/{bucket}"
        if src:
            manager = bucket_manager.get_manager(bucket)
            await manager.send_message(
                Event(cid, local_bucket, bucket, EventName.deleted, src)
            )

        # Remove the file
        remove(file)
        LOG.info(f'File: "{file}" deleted successfully')

        return JSONResponse(
            status_code=200,
            content={
                "info": "File deleted successfully",
                "file": file,
            },
        )
    except OSError as e:
        LOG.error(f"Failed to delete file {file}: {e}", exc_info=True)
        raise DeleteError(
            message=f"Failed to delete file: {filename}",
            details={"bucket": bucket, "filename": filename, "error": str(e)},
        )


LOG.info(f"Module {__name__} loaded")
