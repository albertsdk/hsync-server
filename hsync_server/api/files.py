# HSYNC Server - HTTP-based file synchronization server
# Copyright (C) 2025 Alberto Basso <alberto.sdk [at] gmail [dot] com>
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

This module provides REST API endpoints for file and bucket management:
- Bucket CRUD operations (create, read, delete)
- File upload with integrity verification (MD5)
- File download with streaming response
- File listing with sorting options
- File deletion with event broadcasting

All endpoints require API key authentication via Bearer token.
Rate limiting is applied to protect against abuse.
"""

import logging

from hsync_server.api.utils import compute_md5, is_valid_bucket, is_valid_dir, set_permissions
from hsync_server.api.events import bucket_manager
from hsync_server.config import HSYNC_BUCKETS
from hsync_server.exceptions import (
    BucketNotFoundError,
    ChecksumMismatchError,
    DeleteError,
    FileNotFoundError as HSyncFileNotFoundError,
    InvalidBucketNameError,
    InvalidPathError,
    InvalidPermissionFormatError,
    InvalidSyncOrderError,
    UploadError,
)
from hsync_server.hashes import get_md5_hash, set_md5_hash
from hsync_server.models.file import File as LocalFile
from hsync_server.models.event import Event, EventName
from hsync_server.rate_limiter import (
    limiter,
    RATE_LIMIT_DEFAULT,
    RATE_LIMIT_UPLOAD,
    RATE_LIMIT_DOWNLOAD,
)
from hsync_server.routers import auth_router
from aiofiles.ospath import isfile
from aiofiles.tempfile import NamedTemporaryFile
from datetime import datetime, timezone
from fastapi import File, Form, Request, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse
from os import makedirs, remove, utime, walk
from os.path import (
    abspath,
    commonpath,
    dirname,
    exists,
    getatime,
    getctime,
    getmtime,
)
from re import match
from shutil import move, rmtree
from typing import Optional


# Logger instance for this module (uses __name__ for module-level filtering)
LOG = logging.getLogger(__name__)


def is_safe_path(base_path: str, target_path: str) -> bool:
    """
    Check if a target path is safely within the base path.

    Prevents path traversal attacks by ensuring the target path
    doesn't escape the base directory.

    Args:
        base_path: The base directory path.
        target_path: The target path to validate.

    Returns:
        True if target is within base, False otherwise.
    """
    base_path = abspath(base_path)
    target_path = abspath(target_path)
    return commonpath([base_path]) == commonpath([base_path, target_path])


def is_valid_permission_format(permissions: str) -> bool:
    """
    Validate a symbolic permission string format.

    Args:
        permissions: Permission string to validate (e.g., '-rw-r--r--', 'drwxr-xr-x').

    Returns:
        True if the format is valid, False otherwise.
    """
    # First character: file type (d=dir, l=link, -=file, b=block, c=char, p=pipe, s=socket)
    return bool(match(r"^[dlbcps\-][rwx-]{9}$", permissions))


def update_file_timestamp(
    file_location: str, ctime: Optional[str] = None, mtime: Optional[str] = None
) -> None:
    """
    Update file access and modification timestamps.

    Accepts timestamps as either Unix timestamps (digit strings) or
    ISO format datetime strings.

    Args:
        file_location: Path to the file.
        ctime: Creation/access time (Unix timestamp or ISO string).
        mtime: Modification time (Unix timestamp or ISO string).
    """
    # Only update if at least one timestamp is provided
    if ctime or mtime:
        # Parse ctime (can be Unix timestamp or ISO format)
        if ctime and ctime.isdigit():
            _ctime = int(
                datetime.fromtimestamp(int(ctime), timezone.utc).timestamp()
            )
        elif ctime:
            _ctime = int(datetime.fromisoformat(ctime).timestamp())
        else:
            _ctime = None

        # Parse mtime (can be Unix timestamp or ISO format)
        if mtime and mtime.isdigit():
            _mtime = int(
                datetime.fromtimestamp(int(mtime), timezone.utc).timestamp()
            )
        elif mtime:
            _mtime = int(datetime.fromisoformat(mtime).timestamp())
        else:
            _mtime = None

        LOG.debug(f"Update timestamp: {file_location} ({ctime}, {mtime})")
        # Set access time and modification time (fallback to the other if one is None)
        # Only call utime if we have at least one valid timestamp
        atime = _ctime or _mtime
        mtime_val = _mtime or _ctime
        if atime is not None and mtime_val is not None:
            utime(file_location, (atime, mtime_val))


@auth_router.get(
    "/api/v1/buckets/{bucket}",
    name="Get bucket information",
    summary="Get bucket information",
    description="""Get bucket information including timestamps.""",
)
async def get_bucket(bucket: str) -> JSONResponse:
    """
    Retrieve information about a specific bucket.

    Returns the bucket name and timestamps (access, creation, modification)
    in ISO format.

    Args:
        bucket: Name of the bucket to query.

    Returns:
        JSON response with bucket name and timestamps.

    Raises:
        InvalidBucketNameError: If bucket name is invalid.
        BucketNotFoundError: If bucket does not exist.
    """
    if not is_valid_bucket(bucket):
        raise InvalidBucketNameError(details={"bucket": bucket})

    bucket_dir = f"{HSYNC_BUCKETS}/{bucket}"

    if not exists(bucket_dir):
        raise BucketNotFoundError(details={"bucket": bucket})

    # Fetch and format access, creation and modification times
    times = {
        "adate": getatime(bucket_dir),
        "cdate": getctime(bucket_dir),
        "mdate": getmtime(bucket_dir),
    }
    formatted_times = {
        key: datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
        for key, value in times.items()
    }

    return JSONResponse(
        status_code=status.HTTP_200_OK, content={"name": bucket, **formatted_times}
    )


@auth_router.post("/api/v1/buckets")
async def create_bucket(bucket: str) -> JSONResponse:
    """
    Create a new storage bucket.

    Creates a directory in the buckets storage area with the specified name.
    Bucket names must be alphanumeric.

    Args:
        bucket: Name for the new bucket.

    Returns:
        JSON response confirming creation or conflict if exists.

    Raises:
        InvalidBucketNameError: If bucket name is invalid.
    """
    if not is_valid_bucket(bucket):
        raise InvalidBucketNameError(details={"bucket": bucket})

    bucket_directory = f"{HSYNC_BUCKETS}/{bucket}"

    # Check if bucket already exists
    if exists(bucket_directory):
        LOG.warning(f"Bucket already exists: {bucket_directory}")
        return JSONResponse(
            status_code=409,
            content={
                "info": "Bucket already exists",
                "bucket": bucket,
            },
        )

    makedirs(bucket_directory, exist_ok=True)
    LOG.info(f'Bucket: "{bucket}" created')

    return JSONResponse(
        status_code=201,
        content={
            "info": "Bucket created successfully",
            "bucket": bucket,
        },
    )


@auth_router.delete("/api/v1/buckets/{bucket}")
async def delete_bucket(bucket: str) -> JSONResponse:
    """
    Delete a bucket and all its contents.

    Permanently removes the bucket directory and all files/subdirectories.
    This operation cannot be undone.

    Args:
        bucket: Name of the bucket to delete.

    Returns:
        JSON response confirming deletion.

    Raises:
        InvalidBucketNameError: If bucket name is invalid.
        BucketNotFoundError: If bucket does not exist.
        DeleteError: If deletion fails.
    """
    if not is_valid_bucket(bucket):
        raise InvalidBucketNameError(details={"bucket": bucket})

    bucket_dir = f"{HSYNC_BUCKETS}/{bucket}"

    if not exists(bucket_dir):
        raise BucketNotFoundError(details={"bucket": bucket})

    try:
        # Remove bucket and all contents
        rmtree(bucket_dir)
        LOG.info(f'Bucket: "{bucket}" deleted successfully')

        return JSONResponse(
            status_code=200,
            content={
                "info": "Bucket deleted successfully",
                "bucket": bucket,
            },
        )
    except OSError as e:
        LOG.error(f"Failed to delete bucket {bucket}: {e}", exc_info=True)
        raise DeleteError(
            message=f"Failed to delete bucket: {bucket}",
            details={"bucket": bucket, "error": str(e)},
        )


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
) -> JSONResponse:
    """
    Upload a file to a bucket.

    Handles file upload with integrity verification (MD5), timestamp
    preservation, and permission setting. Broadcasts an "added" event
    to all connected WebSocket clients.

    Args:
        bucket: Target bucket name.
        cid: Client ID for event broadcasting.
        file: The file to upload.
        path: Subdirectory path within the bucket.
        ctime: Creation time (Unix timestamp or ISO string).
        mtime: Modification time (Unix timestamp or ISO string).
        md5sum: Expected MD5 checksum for integrity verification.
        permissions: File permissions in symbolic format.

    Returns:
        JSON response confirming upload or conflict if identical file exists.

    Raises:
        InvalidBucketNameError: If bucket name is invalid.
        InvalidPathError: If path is invalid.
        InvalidPermissionFormatError: If permission format is invalid.
        ChecksumMismatchError: If MD5 checksum verification fails.
        UploadError: If upload operation fails.
    """
    file_location_tmp = None
    try:
        # Validate inputs
        if not is_valid_bucket(bucket):
            raise InvalidBucketNameError(details={"bucket": bucket})
        if not is_valid_dir(path):
            raise InvalidPathError(details={"path": path})
        if permissions and not is_valid_permission_format(permissions):
            raise InvalidPermissionFormatError(details={"permissions": permissions})

        # Define file paths
        file_directory = f"{HSYNC_BUCKETS}/{bucket}/{path}"
        file_location = f"{file_directory}/{file.filename}"

        # Skip upload if identical file already exists (same MD5)
        if md5sum and exists(file_location) and md5sum == get_md5_hash(file_location):
            LOG.warning(f"File already present: {file_location}")
            # Update timestamps even if skipping upload
            update_file_timestamp(file_location, ctime, mtime)
            return JSONResponse(
                status_code=409,
                content={
                    "info": "File already present",
                    "file": file_location,
                },
            )

        # Ensure directory exists
        makedirs(file_directory, exist_ok=True)

        # Write file to temporary location first (atomic write pattern)
        async with NamedTemporaryFile(delete=False) as temp_file:
            file_location_tmp = str(temp_file.name)
            while chunk := await file.read(8192):
                await temp_file.write(chunk)

        # Set timestamps on temporary file
        update_file_timestamp(file_location_tmp, ctime, mtime)

        # Set permissions on temporary file
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
        # Clean up temporary file if it exists
        if file_location_tmp and exists(file_location_tmp):
            remove(file_location_tmp)


@auth_router.get("/api/v1/buckets/{bucket}/files/download")
@limiter.limit(RATE_LIMIT_DOWNLOAD)
async def download_file(
    request: Request, bucket: str, filename: str
) -> FileResponse:
    """
    Download a file from a bucket.

    Streams the file content as a response with appropriate headers.

    Args:
        request: The incoming FastAPI request.
        bucket: Name of the bucket containing the file.
        filename: Path to the file within the bucket.

    Returns:
        FileResponse with the file content for download.

    Raises:
        InvalidBucketNameError: If bucket name is invalid.
        InvalidPathError: If path is invalid.
        HSyncFileNotFoundError: If file does not exist.
    """
    if not is_valid_bucket(bucket):
        raise InvalidBucketNameError(details={"bucket": bucket})
    if not is_valid_dir(filename):
        raise InvalidPathError(details={"path": filename})

    file = f"{HSYNC_BUCKETS}/{bucket}/{filename}"

    if not await isfile(file):
        raise HSyncFileNotFoundError(details={"bucket": bucket, "filename": filename})

    return FileResponse(file, filename=filename)


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
        # Skip hidden directories
        dirs[:] = [d for d in dirs if not d.startswith(".")]

        for file in files:
            # Skip hidden files
            if file.startswith("."):
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

    file = f"{bucket_dir}/{filename}"

    # Validate path to prevent traversal attacks
    if not is_valid_dir(dirname(file)):
        raise InvalidPathError(details={"path": filename})

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
