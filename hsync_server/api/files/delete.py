# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""
File deletion endpoint for HSYNC Server.

This module provides the REST API endpoint for file deletion with:
- Event broadcasting to connected clients
- Sentinel file management
"""

from pathlib import Path
from typing import Annotated

from fastapi import Depends, Form
from fastapi.responses import JSONResponse
from hsync_common.models.event import Event, EventName
from hsync_common.protocol.rest import REST_FILES_SUFFIX, rest_bucket_template
from loguru import logger as log

from hsync_server.api.authz import check_bucket_access
from hsync_server.api.connection_manager import bucket_manager
from hsync_server.api.utils import resolve_within_bucket
from hsync_server.auth import AuthContext, verify_api_key
from hsync_server.config import get_buckets_path
from hsync_server.exceptions import (
    DeleteError,
    InvalidPathError,
)
from hsync_server.exceptions import FileNotFoundError as HSyncFileNotFoundError
from hsync_server.maintenance import cleanup_deleted_path, delete_path_state
from hsync_server.routers import auth_router


@auth_router.delete(rest_bucket_template(REST_FILES_SUFFIX))
async def delete_file(
    bucket: str,
    ctx: Annotated[AuthContext, Depends(verify_api_key)],
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
    check_bucket_access(bucket, ctx)
    # Confine to *this* bucket: the filename is untrusted input and must never
    # escape into a sibling bucket or outside the storage root. The final
    # component is NOT dereferenced: deleting a symlink path removes the link
    # itself, never the target file it points to.
    file = resolve_within_bucket(bucket, filename, resolve_final=False)
    if file is None:
        raise InvalidPathError(details={"path": filename})

    if not Path(file).exists():
        raise HSyncFileNotFoundError(details={"bucket": bucket, "filename": filename})

    local_bucket = f"{get_buckets_path()}/{bucket}"
    try:
        # The state-consistent core is shared with the maintenance sweep: write
        # the causal tombstone, unlink, then drop the hash-cache and version
        # entry. A crash between the two never resurrects the path because the
        # marker is written first.
        src = delete_path_state(bucket, file, bucket_dir=local_bucket, cid=cid)

        # Broadcast "deleted" after the file is gone. Best-effort: the tombstone
        # already guarantees no resurrection if this fails.
        if src:
            manager = bucket_manager.get_manager(bucket)
            await manager.send_message(Event(cid, local_bucket, bucket, EventName.deleted, src))

        # Drop any ``.backup`` snapshot and the emptied directory shell, so the
        # peer that deleted the path does not see the folder advertised back.
        cleanup_deleted_path(file, bucket_dir=local_bucket)

        # Return only the relative path to avoid leaking server-side filesystem
        # layout to the caller.
        return JSONResponse(
            status_code=200,
            content={
                "info": "File deleted successfully",
                "file": filename,
            },
        )
    except OSError as e:
        log.error(f"Failed to delete file {file}: {e}", exc_info=True)
        raise DeleteError(
            message=f"Failed to delete file: {filename}",
            details={"bucket": bucket, "filename": filename, "reason": type(e).__name__},
        )
