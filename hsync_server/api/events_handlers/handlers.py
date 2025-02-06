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
from loguru import logger as log

from hsync_server.api.events_handlers.validation import (
    is_path_within_bucket,
    is_safe_symlink_target,
)
from hsync_server.api.file_utils import (
    cleanup_empty_dir,
    ensure_sentinel_in_dir,
    is_sentinel_file,
)
from hsync_server.api.utils import set_directory_permissions
from hsync_server.hashes import del_sha256_hash
from hsync_server.tombstones import clear_deletion, record_deletion


async def handle_added_event(
    src: dict[str, Any] | None,
    bucket_dir: str,
    bucket: str,
    cid: str,
) -> Event | None:
    """
    Handle 'added' events - create new files/directories.

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

    # Handle directory creation
    if "path" in src:
        dir_obj = Dir().from_dict(src, bucket_dir)
        # Validate path to prevent traversal attacks
        if not dir_obj.path or not is_path_within_bucket(dir_obj.path, bucket_dir):
            log.warning(f'[WEBSOCKET ] Rejected path traversal attempt: "{dir_obj.path}"')
            return None
        if dir_obj.path and dir_obj.permissions:
            log.info(f'[WEBSOCKET ] Added dir "{dir_obj.path}"')
            Path(dir_obj.path).mkdir(parents=True, exist_ok=True)
            await set_directory_permissions(dir_obj.path, dir_obj.permissions)
            return Event(cid, bucket_dir, bucket, EventName.added, dir_obj)

    # Handle file creation notification
    elif "filepath" in src:
        file_obj = File().from_dict(src, bucket_dir, False)
        # Validate path to prevent traversal attacks
        if file_obj.filepath is None or not is_path_within_bucket(file_obj.filepath, bucket_dir):
            log.warning(f'[WEBSOCKET ] Rejected path traversal attempt: "{file_obj.filepath}"')
            return None
        log.info(f'[WEBSOCKET ] Added file "{file_obj.filepath}"')
        # A (re-)created file invalidates any prior deletion marker so peers
        # do not delete the freshly added file on their next full sync.
        if not is_sentinel_file(file_obj.filepath):
            clear_deletion(bucket, bucket_dir, file_obj.filepath)
        return Event(cid, bucket_dir, bucket, EventName.added, file_obj)

    return None


async def handle_modified_event(
    src: dict[str, Any] | None,
    bucket_dir: str,
    bucket: str,
    cid: str,
) -> Event | None:
    """
    Handle 'modified' events - update existing files/directories.

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

    # Handle directory modification (permissions)
    if "path" in src:
        dir_obj = Dir().from_dict(src, bucket_dir)
        # Validate path to prevent traversal attacks
        if dir_obj.path is None or not is_path_within_bucket(dir_obj.path, bucket_dir):
            log.warning(f'[WEBSOCKET ] Rejected path traversal attempt: "{dir_obj.path}"')
            return None
        if dir_obj.path and dir_obj.permissions:
            log.info(f'[WEBSOCKET ] Modified dir "{dir_obj.path}"')
            await set_directory_permissions(dir_obj.path, dir_obj.permissions)
            return Event(cid, bucket_dir, bucket, EventName.modified, dir_obj)

    # Handle file modification notification
    elif "filepath" in src:
        file_obj = File().from_dict(src, bucket_dir, False)
        # Validate path to prevent traversal attacks
        if file_obj.filepath is None or not is_path_within_bucket(file_obj.filepath, bucket_dir):
            log.warning(f'[WEBSOCKET ] Rejected path traversal attempt: "{file_obj.filepath}"')
            return None
        log.info(f'[WEBSOCKET ] Modified file "{file_obj.filepath}"')
        if not is_sentinel_file(file_obj.filepath):
            clear_deletion(bucket, bucket_dir, file_obj.filepath)
        return Event(cid, bucket_dir, bucket, EventName.modified, file_obj)

    return None


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
        # Validate path to prevent traversal attacks. allow_root=False
        # also rejects an attempt to rmtree the bucket directory itself
        # (a malicious or buggy client could issue a delete on the root).
        if dir_obj.path is None or not is_path_within_bucket(
            dir_obj.path, bucket_dir, allow_root=False
        ):
            log.warning(f'[WEBSOCKET ] Rejected path traversal attempt: "{dir_obj.path}"')
            return None
        if dir_obj.path:
            if not Path(dir_obj.path).exists():
                log.warning(f'[WEBSOCKET ] Dir: "{dir_obj.path}" not found.')
            else:
                log.info(f'[WEBSOCKET ] Deleted dir "{dir_obj.path}"')
                rmtree(dir_obj.path, ignore_errors=False)
            return Event(cid, bucket_dir, bucket, EventName.deleted, dir_obj)

    # Handle file deletion
    elif "filepath" in src:
        file_obj = File().from_dict(src, bucket_dir, False)
        # Validate path to prevent traversal attacks
        if file_obj.filepath is None or not is_path_within_bucket(file_obj.filepath, bucket_dir):
            log.warning(f'[WEBSOCKET ] Rejected path traversal attempt: "{file_obj.filepath}"')
            return None
        if file_obj.filepath:
            if not Path(file_obj.filepath).exists():
                log.warning(f'[WEBSOCKET ] File: "{file_obj.filepath}" not found.')
            else:
                log.info(f'[WEBSOCKET ] Deleted file "{file_obj.filepath}"')
                remove(file_obj.filepath)
                parent = str(Path(file_obj.filepath).parent)
                if is_sentinel_file(file_obj.filepath):
                    # Sentinel removed — clean up the now-empty directory.
                    # Never remove the bucket root itself.
                    cleanup_empty_dir(parent, bucket_root=bucket_dir)
                else:
                    # Regular file removed — preserve directory with sentinel
                    ensure_sentinel_in_dir(parent)
            # Remove from hash cache
            del_sha256_hash(file_obj.filepath)
            # Record a tombstone so peers offline at deletion time do not
            # resurrect the file on their next full sync.
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
        # Validate both paths to prevent traversal attacks
        if src_dir.path is None or not is_path_within_bucket(src_dir.path, bucket_dir):
            log.warning(f'[WEBSOCKET ] Rejected path traversal attempt: "{src_dir.path}"')
            return None
        if dst_dir.path is None or not is_path_within_bucket(dst_dir.path, bucket_dir):
            log.warning(f'[WEBSOCKET ] Rejected path traversal attempt: "{dst_dir.path}"')
            return None
        if src_dir.path and dst_dir.path:
            log.info(f'[WEBSOCKET ] Renamed "{src_dir.path}" -> "{dst_dir.path}"')
            rename(src_dir.path, dst_dir.path)
            return Event(cid, bucket_dir, bucket, EventName.renamed, src_dir, dst_dir)

    # Handle file rename
    elif src.get("filepath"):
        src_file = File().from_dict(src, bucket_dir, False)
        dst_file = File().from_dict(dst, bucket_dir, False)
        # Validate both paths to prevent traversal attacks
        if src_file.filepath is None or not is_path_within_bucket(src_file.filepath, bucket_dir):
            log.warning(f'[WEBSOCKET ] Rejected path traversal attempt: "{src_file.filepath}"')
            return None
        if dst_file.filepath is None or not is_path_within_bucket(dst_file.filepath, bucket_dir):
            log.warning(f'[WEBSOCKET ] Rejected path traversal attempt: "{dst_file.filepath}"')
            return None
        if src_file.filepath and dst_file.filepath:
            log.info(f'[WEBSOCKET ] Renamed "{src_file.filepath}" -> "{dst_file.filepath}"')
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
        link = Symlink.from_dict(src)
        # Build the full path
        link.filepath = f"{bucket_dir}/{link.filepath.lstrip('/')}"
        target = link.target

        if link.filepath and target:
            # Validate symlink path to prevent path traversal attacks
            if not is_path_within_bucket(link.filepath, bucket_dir):
                log.warning(f'[WEBSOCKET ] Rejected path traversal attempt: "{link.filepath}"')
                return None

            # Validate symlink target to prevent path traversal attacks
            if not is_safe_symlink_target(link.filepath, target, bucket_dir):
                log.warning(
                    f'[WEBSOCKET ] Rejected unsafe symlink "{link.filepath}" -> "{target}" '
                    f"(target escapes bucket directory)"
                )
                return None

            log.info(f'[WEBSOCKET ] Symlink "{link.filepath}" -> "{target}"')
            # Ensure parent directory exists
            parent_dir = str(Path(link.filepath).parent)
            if not Path(parent_dir).exists():
                Path(parent_dir).mkdir(parents=True, exist_ok=True)

            # Remove existing symlink or file if present
            if Path(link.filepath).is_symlink() or Path(link.filepath).exists():
                unlink(link.filepath)

            # Create the symlink
            os_symlink(target, link.filepath)
            return Event(cid, bucket_dir, bucket, EventName.symlink, link)

    return None
