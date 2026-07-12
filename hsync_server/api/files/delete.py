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

from pathlib import Path
from typing import Annotated

from fastapi import Depends, Form
from fastapi.responses import JSONResponse
from hsync_common.models.event import Event, EventName, is_valid_cid
from hsync_common.models.file import File as LocalFile
from hsync_common.models.file import invalidate_sha256_cache
from hsync_common.models.tombstone import relpath_in_bucket
from loguru import logger as log

from hsync_server.api.authz import check_bucket_access
from hsync_server.api.connection_manager import bucket_manager
from hsync_server.api.file_utils import (
    cleanup_empty_dir,
    ensure_sentinel_in_dir,
    is_sentinel_file,
)
from hsync_server.api.utils import resolve_within_bucket
from hsync_server.auth import AuthContext, verify_api_key
from hsync_server.config import get_buckets_path
from hsync_server.constants import BACKUP_SUFFIX, ActionLabel
from hsync_server.exceptions import (
    DeleteError,
    InvalidPathError,
)
from hsync_server.exceptions import FileNotFoundError as HSyncFileNotFoundError
from hsync_server.routers import auth_router
from hsync_server.tombstones import clear_deletion, record_deletion
from hsync_server.versions import clear_version, get_version_store


@auth_router.delete("/api/v1/buckets/{bucket}/files")
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
    # Confine to *this* bucket: the filename is untrusted input and must
    # never escape into a sibling bucket or outside the storage root. The
    # final component is NOT dereferenced: deleting a symlink path removes
    # the link itself, never the target file it points to.
    file = resolve_within_bucket(bucket, filename, resolve_final=False)
    if file is None:
        raise InvalidPathError(details={"path": filename})

    if not Path(file).exists():
        raise HSyncFileNotFoundError(details={"bucket": bucket, "filename": filename})

    try:
        from os import remove

        # Collect file metadata before deletion (needed for the event and tombstone).
        # Kept synchronous ON PURPOSE: moved to a worker thread it would
        # re-enable event-loop interleaving between metadata capture, tombstone
        # persistence and the unlink — the delete must stay one atomic step
        # w.r.t. other coroutines.
        src = LocalFile.get(file)
        local_bucket = f"{get_buckets_path()}/{bucket}"

        # Record a causal tombstone BEFORE removing the file so peers offline
        # at deletion time do not resurrect the file on their next full sync.
        # The marker inherits the stored version clock for the path and is
        # ticked by the requesting peer (validated), so delete-vs-recreate and
        # delete-vs-delete resolve causally (ADR-0003). Writing the marker
        # first guarantees that a server crash between deletion and tombstone
        # persistence never causes a resurrection. Sentinel files are per-peer
        # and never exchanged — no marker needed.
        if not is_sentinel_file(file):
            rel = relpath_in_bucket(file, local_bucket)
            stored = get_version_store(bucket).get(rel) if rel is not None else None
            record_deletion(
                bucket,
                local_bucket,
                file,
                sha256sum=src.sha256sum if src else None,
                origin_cid=cid if is_valid_cid(cid) else None,
                prev_vclock=stored["vclock"] if stored else None,
            )
        else:
            # Sentinel files are per-peer helper artifacts: when one is removed
            # the path is only expected to carry directory-state sentinels, not
            # user content; a deletion marker would outlive the sentinel and
            # block its future re-creation. Drop any stale marker instead.
            clear_deletion(bucket, local_bucket, file)

        # Remove the file after the tombstone is persisted. If the remove
        # fails, the tombstone is harmless (the file path still exists,
        # and a subsequent re-upload clears the marker via clear_deletion).
        remove(file)
        invalidate_sha256_cache(file)
        # Drop the causal version entry so a re-created file starts fresh.
        clear_version(bucket, local_bucket, file)
        log.info(f'{ActionLabel.DELETE} File: "{file}" deleted successfully (CID: {cid})')

        # Broadcast "deleted" event to other clients after successful removal.
        # This is best-effort: a crash here won't resurrect the file because
        # the tombstone is already on disk (written above).
        if src:
            manager = bucket_manager.get_manager(bucket)
            await manager.send_message(Event(cid, local_bucket, bucket, EventName.deleted, src))

        # Also remove any backup file associated with this file
        backup_file = f"{file}{BACKUP_SUFFIX}"
        if Path(backup_file).exists():
            remove(backup_file)
            log.info(f'{ActionLabel.DELETE} Backup file: "{backup_file}" deleted successfully')

        parent = str(Path(file).parent)
        if is_sentinel_file(file):
            # Sentinel removed — clean up the now-empty directory.
            # bucket_root must never be removed: a sentinel deletion at the
            # bucket root would otherwise wipe the whole bucket folder.
            cleanup_empty_dir(parent, bucket_root=local_bucket)
        else:
            # Regular file removed — preserve directory with sentinel
            ensure_sentinel_in_dir(parent)

        # Return only the relative path to avoid leaking server-side
        # filesystem layout to the caller.
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
            details={"bucket": bucket, "filename": filename, "error": str(e)},
        )
