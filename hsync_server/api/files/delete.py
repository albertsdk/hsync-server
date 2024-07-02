# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
File deletion endpoint for HSYNC Server.

This module provides the REST API endpoint for file deletion with:
- Event broadcasting to connected clients
- Sentinel file management
"""

from os.path import dirname, exists

from fastapi import Form
from fastapi.responses import JSONResponse
from loguru import logger as log

from hsync_server.api.connection_manager import bucket_manager
from hsync_server.api.file_utils import (
    cleanup_empty_dir,
    ensure_sentinel_in_dir,
    is_sentinel_file,
)
from hsync_server.api.utils import is_valid_bucket, is_valid_dir
from hsync_server.config import HSYNC_BUCKETS
from hsync_server.exceptions import (
    BucketNotFoundError,
    DeleteError,
    InvalidBucketNameError,
    InvalidPathError,
)
from hsync_server.exceptions import FileNotFoundError as HSyncFileNotFoundError
from hsync_server.models.event import Event, EventName
from hsync_server.models.file import File as LocalFile
from hsync_server.models.file import invalidate_md5_cache
from hsync_server.routers import auth_router


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
        from os import remove

        # Broadcast "deleted" event to other clients before deletion
        src = LocalFile().get(file)
        local_bucket = f"{HSYNC_BUCKETS}/{bucket}"
        if src:
            manager = bucket_manager.get_manager(bucket)
            await manager.send_message(Event(cid, local_bucket, bucket, EventName.deleted, src))

        # Remove the file
        remove(file)
        invalidate_md5_cache(file)
        log.info(f'File: "{file}" deleted successfully')

        # Also remove any backup file associated with this file
        backup_file = f"{file}.backup"
        if exists(backup_file):
            remove(backup_file)
            log.info(f'Backup file: "{backup_file}" deleted successfully')

        parent = dirname(file)
        if is_sentinel_file(file):
            # Sentinel removed — clean up the now-empty directory
            cleanup_empty_dir(parent)
        else:
            # Regular file removed — preserve directory with sentinel
            ensure_sentinel_in_dir(parent)

        return JSONResponse(
            status_code=200,
            content={
                "info": "File deleted successfully",
                "file": file,
            },
        )
    except OSError as e:
        log.error(f"Failed to delete file {file}: {e}", exc_info=True)
        raise DeleteError(
            message=f"Failed to delete file: {filename}",
            details={"bucket": bucket, "filename": filename, "error": str(e)},
        )
