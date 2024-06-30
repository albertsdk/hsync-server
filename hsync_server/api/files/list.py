# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
File listing and upload status endpoints for HSYNC Server.

This module provides the REST API endpoints for:
- Listing files in a bucket with sorting
- Getting upload status for resumable uploads
"""

from os import remove, walk
from os.path import dirname, exists, getsize

from fastapi import Request
from fastapi.responses import JSONResponse

from hsync_server.api.utils import is_valid_bucket, is_valid_dir
from hsync_server.config import HSYNC_BUCKETS
from hsync_server.exceptions import (
    BucketNotFoundError,
    InvalidBucketNameError,
    InvalidPathError,
)
from hsync_server.hashes import get_md5_hash
from hsync_server.models.dir import is_hidden_dir
from hsync_server.models.file import File as LocalFile
from hsync_server.models.file import is_hidden_file
from hsync_server.rate_limiter import RATE_LIMIT_DEFAULT, limiter
from hsync_server.routers import auth_router


@auth_router.get("/api/v1/buckets/{bucket}/files/upload-status")
@limiter.limit(RATE_LIMIT_DEFAULT)
async def get_upload_status(
    request: Request,  # Required by rate limiter
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
    request: Request,  # Required by rate limiter
    bucket: str,
    path: str | None = None,
) -> JSONResponse:
    """
    List all files in a bucket or subdirectory.

    Recursively walks the directory tree and returns metadata for
    all non-hidden files, sorted by mtime ascending (oldest first)
    to prioritize updating older files.

    Args:
        bucket: Name of the bucket to list files from.
        path: Optional subdirectory path within the bucket.

    Returns:
        JSON response with list of file metadata dictionaries.

    Raises:
        InvalidBucketNameError: If bucket name is invalid.
        InvalidPathError: If path is invalid.
        BucketNotFoundError: If bucket does not exist.
    """
    # Validate inputs
    if not is_valid_bucket(bucket):
        raise InvalidBucketNameError(details={"bucket": bucket})
    if path and not is_valid_dir(path):
        raise InvalidPathError(details={"path": path})

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
            # Skip backup files (used during upload to maintain read access)
            if file.endswith(".backup"):
                continue

            # Get file info and normalize path relative to bucket
            file_obj = LocalFile().get(file, root)
            if file_obj:
                file_info = file_obj.to_dict()
                file_data = LocalFile().from_dict(file_info, f"{HSYNC_BUCKETS}/{bucket}").to_dict()
                file_list.append(file_data)

    # Sort by mtime ascending (oldest first) to prioritize updating older files
    file_list.sort(key=lambda x: x.get("mtime", 0))

    return JSONResponse(
        status_code=200,
        content=file_list,
    )
