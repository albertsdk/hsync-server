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

from hsync_server.api.utils import compute_md5, is_valid_bucket, is_valid_dir, set_permissions
from hsync_server.api.events import bucket_manager
from hsync_server.config import HSYNC_BUCKETS
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
from fastapi import File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse
from logging import getLogger
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


# Logger instance for this module
LOG = getLogger("main")


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
        HTTPException: 404 if bucket not found, 400 for invalid bucket name.
    """
    try:
        if not is_valid_bucket(bucket):
            raise ValueError("Invalid bucket name")

        bucket_dir = f"{HSYNC_BUCKETS}/{bucket}"

        if not exists(bucket_dir):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Bucket not found"
            )

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
    except HTTPException as e:
        LOG.error(f"Failed to get bucket information: {bucket}: {e}", exc_info=True)
        raise e
    except Exception as e:
        LOG.error(f"Failed to get bucket information: {bucket}: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


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
        HTTPException: 400 for invalid bucket name or other errors.
    """
    try:
        if not is_valid_bucket(bucket):
            raise ValueError("Invalid bucket name")

        bucket_directory = f"{HSYNC_BUCKETS}/{bucket}"

        # Check if bucket already exists
        if exists(bucket_directory):
            LOG.warning(f"Bucket already exists: {bucket_directory}")
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content={
                    "info": "Bucket already exists",
                    "bucket": bucket,
                },
            )
        else:
            makedirs(bucket_directory, exist_ok=True)
            LOG.info(f'Bucket: "{bucket}" created')

        return JSONResponse(
            status_code=status.HTTP_201_CREATED,
            content={
                "info": "Bucket created successfully",
                "bucket": bucket,
            },
        )
    except Exception as e:
        LOG.error(f"Bucket creation failed: {bucket}: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


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
        HTTPException: 400 for invalid name, 404 if not found, 500 for errors.
    """
    bucket_dir = f"{HSYNC_BUCKETS}/{bucket}"
    try:
        if not is_valid_bucket(bucket):
            LOG.error(f"Invalid bucket name: {bucket}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid bucket name"
            )
        if not exists(bucket_dir):
            LOG.warning(f"Bucket not found: {bucket_dir}")
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Bucket not found"
            )

        # Remove bucket and all contents
        rmtree(bucket_dir)
        LOG.info(f'Bucket: "{bucket}" deleted successfully')

        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "info": "Bucket deleted successfully",
                "bucket": bucket,
            },
        )
    except HTTPException as e:
        LOG.error(f"Failed to delete bucket: {bucket}: {e}", exc_info=True)
        raise e
    except Exception as e:
        LOG.error(
            f"Unexpected error while deleting bucket {bucket}: {e}", exc_info=True
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete bucket",
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
        HTTPException: 400 for validation errors or upload failures.
    """
    file_location_tmp = None
    try:
        # Validate inputs
        if not is_valid_bucket(bucket):
            raise ValueError("Invalid bucket name")
        if not is_valid_dir(path):
            raise ValueError("Invalid path")
        if permissions and not is_valid_permission_format(permissions):
            raise ValueError("Invalid permission format")

        # Define file paths
        file_directory = f"{HSYNC_BUCKETS}/{bucket}/{path}"
        file_location = f"{file_directory}/{file.filename}"

        # Skip upload if identical file already exists (same MD5)
        if md5sum and exists(file_location) and md5sum == get_md5_hash(file_location):
            LOG.warning(f"File already present: {file_location}")
            # Update timestamps even if skipping upload
            update_file_timestamp(file_location, ctime, mtime)
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
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
            error = f"Invalid checksum: expected {md5sum}, {md5sum_tmp} given"
            LOG.error(error)
            raise ValueError(error)

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
            status_code=status.HTTP_201_CREATED,
            content={
                "info": "File uploaded successfully",
                "file": f"{bucket}/{path}/{file.filename}",
            },
        )
    except Exception as e:
        LOG.error(f"Upload failed for file {file.filename}: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
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
        HTTPException: 404 if file not found, 400 for validation errors.
    """
    try:
        if not is_valid_bucket(bucket):
            raise ValueError("Invalid bucket name")
        if not is_valid_dir(filename):
            raise ValueError("Invalid path")

        file = f"{HSYNC_BUCKETS}/{bucket}/{filename}"

        if not await isfile(file):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="File not found"
            )
        return FileResponse(file, filename=filename)
    except HTTPException as e:
        LOG.error(f"Failed to download file: {bucket}:{filename} {e}", exc_info=True)
        raise e
    except Exception as e:
        LOG.error(f"Download failed {filename}: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


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
        HTTPException: 404 if bucket not found, 400 for validation errors.
    """
    try:
        # Validate inputs
        if not is_valid_bucket(bucket):
            raise ValueError("Invalid bucket name")
        if path and not is_valid_dir(path):
            raise ValueError("Invalid path")

        valid_orders = ["mtime", "ctime"]
        if sync_order not in valid_orders:
            raise ValueError(
                f"Invalid order '{sync_order}'. Must be one of {valid_orders}."
            )

        # Construct directory path
        dir = f"{HSYNC_BUCKETS}/{bucket}/{path or ''}".rstrip("/")

        if not exists(dir):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Bucket not found: {bucket}",
            )

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
            status_code=status.HTTP_200_OK,
            content=file_list,
        )
    except HTTPException as e:
        LOG.error(f"Files list failed: {e}", exc_info=True)
        raise e
    except Exception as e:
        LOG.error(f"Files list failed: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


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
        HTTPException: 400 for invalid input, 404 if not found, 500 for errors.
    """
    # Validate bucket name
    if not is_valid_bucket(bucket):
        LOG.error(f"Invalid bucket name: {bucket}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid bucket name"
        )

    bucket_dir = f"{HSYNC_BUCKETS}/{bucket}"
    if not exists(bucket_dir):
        LOG.warning(f"Bucket not found: {bucket_dir}")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Bucket not found"
        )

    file = f"{bucket_dir}/{filename}"

    # Validate path to prevent traversal attacks
    if not is_valid_dir(dirname(file)):
        LOG.error(f"Invalid path: {bucket}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid path"
        )

    try:
        if not exists(file):
            LOG.warning(f"File not found: {file}")
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="File not found"
            )

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
            status_code=status.HTTP_200_OK,
            content={
                "info": "File deleted successfully",
                "file": file,
            },
        )
    except HTTPException as e:
        LOG.error(f"Error deleting file {file}: {e}", exc_info=True)
        raise e
    except Exception as e:
        LOG.error(f"Unexpected error while deleting file {file}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete file",
        )


LOG.info(f"Module {__name__} loaded")
