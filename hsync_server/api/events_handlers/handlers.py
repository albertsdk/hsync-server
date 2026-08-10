# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
WebSocket event handlers for HSYNC Server.

This module provides handler functions for different event types:
- handle_added_event: Create new files/directories
- handle_modified_event: Update existing files/directories
- handle_deleted_event: Remove files/directories
- handle_renamed_event: Rename/move files or directories
- handle_symlink_event: Create or update symbolic links
"""

from os import remove, rename, unlink
from os import symlink as os_symlink
from pathlib import Path
from shutil import rmtree
from typing import Any

from hsync_common.models.dir import Dir
from hsync_common.models.event import Event, EventName
from hsync_common.models.file import File
from hsync_common.models.symlink import Symlink
from hsync_common.path_validation import is_safe_symlink_target
from loguru import logger as log

from hsync_server.api.events_handlers.validation import (
    is_path_within_bucket,
)
from hsync_server.api.file_utils import (
    cleanup_empty_dir,
    ensure_sentinel_in_dir,
    is_sentinel_file,
)
from hsync_server.api.utils import set_directory_permissions
from hsync_server.constants import ActionLabel
from hsync_server.hashes import del_sha256_hash
from hsync_server.tombstones import clear_deletion, record_deletion

# ---------------------------------------------------------------------------
# Shared validation helpers (DRY: replaces 8 duplicated path-validation blocks)
# ---------------------------------------------------------------------------


def _validate_path(path: str | None, bucket_dir: str, *, allow_root: bool = True) -> bool:
    """Return False when *path* is missing or escapes *bucket_dir*, logging the attempt."""
    if path is None or not is_path_within_bucket(path, bucket_dir, allow_root=allow_root):
        log.warning(f"{ActionLabel.SECURITY} Rejected path traversal attempt: {path!r}")
        return False
    return True


# ---------------------------------------------------------------------------
# Shared create-or-modify handler (DRY: added + modified were ~90% identical)
# ---------------------------------------------------------------------------

_CREATE = "create"
_MODIFY = "modify"


async def _handle_create_or_modify(
    src: dict[str, Any] | None,
    bucket_dir: str,
    bucket: str,
    cid: str,
    event_name: EventName,
    *,
    create_dir: bool = False,
) -> Event | None:
    """Common logic for ``added`` and ``modified`` events."""
    if not src:
        return None

    if "path" in src:
        dir_obj = Dir().from_dict(src, bucket_dir)
        if not _validate_path(dir_obj.path, bucket_dir):
            return None
        if dir_obj.path and dir_obj.permissions:
            log.info(
                f"{ActionLabel.CREATE if create_dir else ActionLabel.MODIFY} "
                f'{"Added" if create_dir else "Modified"} dir "{dir_obj.path}" (CID: {cid})'
            )
            if create_dir:
                Path(dir_obj.path).mkdir(parents=True, exist_ok=True)
            await set_directory_permissions(dir_obj.path, dir_obj.permissions)
            return Event(cid, bucket_dir, bucket, event_name, dir_obj)

    elif "filepath" in src:
        file_obj = File().from_dict(src, bucket_dir, False)
        if not _validate_path(file_obj.filepath, bucket_dir):
            return None
        log.info(
            f"{ActionLabel.CREATE if create_dir else ActionLabel.MODIFY} "
            f'{"Added" if create_dir else "Modified"} file "{file_obj.filepath}" (CID: {cid})'
        )
        # A (re-)created file invalidates any prior deletion marker so peers
        # do not delete the freshly added file on their next full sync.
        if not is_sentinel_file(file_obj.filepath):
            clear_deletion(bucket, bucket_dir, file_obj.filepath)
        return Event(cid, bucket_dir, bucket, event_name, file_obj)

    return None


# ---------------------------------------------------------------------------
# Public event handlers
# ---------------------------------------------------------------------------


async def handle_added_event(
    src: dict[str, Any] | None,
    bucket_dir: str,
    bucket: str,
    cid: str,
) -> Event | None:
    """Handle 'added' events — create new files/directories."""
    return await _handle_create_or_modify(
        src,
        bucket_dir,
        bucket,
        cid,
        EventName.added,
        create_dir=True,
    )


async def handle_modified_event(
    src: dict[str, Any] | None,
    bucket_dir: str,
    bucket: str,
    cid: str,
) -> Event | None:
    """Handle 'modified' events — update existing files/directories."""
    return await _handle_create_or_modify(
        src,
        bucket_dir,
        bucket,
        cid,
        EventName.modified,
        create_dir=False,
    )


async def handle_deleted_event(
    src: dict[str, Any] | None,
    bucket_dir: str,
    bucket: str,
    cid: str,
) -> Event | None:
    """
    Handle 'deleted' events - remove files/directories.

    Args:
        src: Source object data from the event.
        bucket_dir: Full path to the bucket directory.
        bucket: Bucket name.
        cid: Client ID.

    Returns:
        Event object if successful, None otherwise.
    """
    if not src:
        return None

    # Handle directory deletion
    if "path" in src:
        dir_obj = Dir().from_dict(src, bucket_dir)
        # allow_root=False rejects an attempt to rmtree the bucket directory itself
        if not _validate_path(dir_obj.path, bucket_dir, allow_root=False):
            return None
        if dir_obj.path:
            if not Path(dir_obj.path).exists():
                log.warning(f'{ActionLabel.DELETE} Dir: "{dir_obj.path}" not found.')
            else:
                log.info(f'{ActionLabel.DELETE} Deleted dir "{dir_obj.path}" (CID: {cid})')
                rmtree(dir_obj.path, ignore_errors=False)
            return Event(cid, bucket_dir, bucket, EventName.deleted, dir_obj)

    # Handle file deletion
    elif "filepath" in src:
        file_obj = File().from_dict(src, bucket_dir, False)
        if not _validate_path(file_obj.filepath, bucket_dir):
            return None
        if file_obj.filepath:
            if not Path(file_obj.filepath).exists():
                log.warning(f'{ActionLabel.DELETE} File: "{file_obj.filepath}" not found.')
            else:
                log.info(f'{ActionLabel.DELETE} Deleted file "{file_obj.filepath}" (CID: {cid})')
                remove(file_obj.filepath)
                parent = str(Path(file_obj.filepath).parent)
                if is_sentinel_file(file_obj.filepath):
                    cleanup_empty_dir(parent, bucket_root=bucket_dir)
                else:
                    ensure_sentinel_in_dir(parent)
            del_sha256_hash(file_obj.filepath)
            if not is_sentinel_file(file_obj.filepath):
                record_deletion(
                    bucket,
                    bucket_dir,
                    file_obj.filepath,
                    sha256sum=file_obj.sha256sum,
                )
            return Event(cid, bucket_dir, bucket, EventName.deleted, file_obj)

    return None


async def handle_renamed_event(
    src: dict[str, Any] | None,
    dst: dict[str, Any] | None,
    bucket_dir: str,
    bucket: str,
    cid: str,
) -> Event | None:
    """
    Handle 'renamed' events - rename/move files or directories.

    Args:
        src: Source object data from the event.
        dst: Destination object data from the event.
        bucket_dir: Full path to the bucket directory.
        bucket: Bucket name.
        cid: Client ID.

    Returns:
        Event object if successful, None otherwise.
    """
    if not src or not dst:
        return None

    # Handle directory rename
    if src.get("path"):
        src_dir = Dir().from_dict(src, bucket_dir)
        dst_dir = Dir().from_dict(dst, bucket_dir)
        if not _validate_path(src_dir.path, bucket_dir):
            return None
        if not _validate_path(dst_dir.path, bucket_dir):
            return None
        if src_dir.path and dst_dir.path:
            log.info(
                f'{ActionLabel.RENAME} Renamed "{src_dir.path}" -> "{dst_dir.path}" (CID: {cid})'
            )
            rename(src_dir.path, dst_dir.path)
            return Event(cid, bucket_dir, bucket, EventName.renamed, src_dir, dst_dir)

    # Handle file rename
    elif src.get("filepath"):
        src_file = File().from_dict(src, bucket_dir, False)
        dst_file = File().from_dict(dst, bucket_dir, False)
        if not _validate_path(src_file.filepath, bucket_dir):
            return None
        if not _validate_path(dst_file.filepath, bucket_dir):
            return None
        if src_file.filepath and dst_file.filepath:
            log.info(
                f'{ActionLabel.RENAME} Renamed "{src_file.filepath}" -> "{dst_file.filepath}" (CID: {cid})'
            )
            rename(src_file.filepath, dst_file.filepath)
            return Event(cid, bucket_dir, bucket, EventName.renamed, src_file, dst_file)

    return None


async def handle_symlink_event(
    src: dict[str, Any] | None,
    bucket_dir: str,
    bucket: str,
    cid: str,
) -> Event | None:
    """
    Handle 'symlink' events - create or update symbolic links.

    Args:
        src: Source object data from the event.
        bucket_dir: Full path to the bucket directory.
        bucket: Bucket name.
        cid: Client ID.

    Returns:
        Event object if successful, None otherwise.
    """
    if not src:
        return None

    if "type" in src and src.get("type") == "symlink":
        link = Symlink.from_dict(src, strip_dir_prefix=False)
        link.filepath = f"{bucket_dir}/{link.filepath.lstrip('/')}"
        target = link.target

        if link.filepath and target:
            if not _validate_path(link.filepath, bucket_dir):
                return None

            if not is_safe_symlink_target(link.filepath, target, bucket_dir):
                log.warning(
                    f"{ActionLabel.SECURITY} Rejected unsafe symlink {link.filepath!r} -> {target!r} "
                    f"(target escapes bucket directory)"
                )
                return None

            log.info(f'{ActionLabel.SYMLINK} Symlink "{link.filepath}" -> "{target}" (CID: {cid})')
            parent_dir = str(Path(link.filepath).parent)
            if not Path(parent_dir).exists():
                Path(parent_dir).mkdir(parents=True, exist_ok=True)

            if Path(link.filepath).is_symlink() or Path(link.filepath).exists():
                unlink(link.filepath)

            os_symlink(target, link.filepath)
            return Event(cid, bucket_dir, bucket, EventName.symlink, link)

    return None
