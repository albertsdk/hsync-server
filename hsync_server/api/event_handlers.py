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

from loguru import logger as LOG
from os import makedirs, rename, remove, symlink as os_symlink, unlink
from os.path import dirname, exists, islink
from pathlib import Path
from shutil import rmtree
from typing import Any, Dict, Optional

from hsync_server.api.utils import set_permissions
from hsync_server.hashes import del_md5_hash
from hsync_server.models.dir import Dir
from hsync_server.models.event import Event, EventName
from hsync_server.models.file import File
from hsync_server.models.symlink import Symlink


def is_path_within_bucket(path: str, bucket_dir: str) -> bool:
    """
    Validate that a path is within the bucket directory.

    Prevents path traversal attacks where a malicious client could
    access files outside the bucket (e.g., /etc/passwd).

    Args:
        path: Full path to validate.
        bucket_dir: The bucket directory that should contain all files.

    Returns:
        True if the path is within the bucket, False otherwise.
    """
    if not path:
        return False
    bucket_path = Path(bucket_dir).resolve()
    target_path = Path(path).resolve()
    try:
        target_path.relative_to(bucket_path)
        return True
    except ValueError:
        return False


def is_safe_symlink_target(symlink_path: str, target: str, bucket_dir: str) -> bool:
    """
    Validate that a symlink target is safe and within the bucket directory.

    Prevents symlink attacks where a malicious client could create symlinks
    pointing to sensitive files outside the bucket (e.g., /etc/passwd).

    Args:
        symlink_path: Full path where the symlink will be created.
        target: The target path the symlink will point to.
        bucket_dir: The bucket directory that should contain all files.

    Returns:
        True if the target is safe (within bucket), False otherwise.
    """
    bucket_path = Path(bucket_dir).resolve()

    # If target is absolute, it must be within the bucket
    if Path(target).is_absolute():
        target_resolved = Path(target).resolve()
    else:
        # For relative targets, resolve from the symlink's parent directory
        symlink_parent = Path(symlink_path).parent
        target_resolved = (symlink_parent / target).resolve()

    # Check if the resolved target is within the bucket directory
    try:
        target_resolved.relative_to(bucket_path)
        return True
    except ValueError:
        return False


async def handle_added_event(
    src: Optional[Dict[str, Any]],
    bucket_dir: str,
    bucket: str,
    cid: str,
) -> Optional[Event]:
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
            LOG.warning(f'[WEBSOCKET ] Rejected path traversal attempt: "{dir_obj.path}"')
            return None
        if dir_obj.path and dir_obj.permissions:
            LOG.info(f'[WEBSOCKET ] Added dir "{dir_obj.path}"')
            makedirs(dir_obj.path, exist_ok=True)
            await set_permissions(dir_obj.path, dir_obj.permissions)
            return Event(cid, bucket_dir, bucket, EventName.added, dir_obj)

    # Handle file creation notification
    elif "filepath" in src:
        file_obj = File().from_dict(src, bucket_dir, False)
        # Validate path to prevent traversal attacks
        if file_obj.filepath is None or not is_path_within_bucket(file_obj.filepath, bucket_dir):
            LOG.warning(f'[WEBSOCKET ] Rejected path traversal attempt: "{file_obj.filepath}"')
            return None
        LOG.info(f'[WEBSOCKET ] Added file "{file_obj.filepath}"')
        return Event(cid, bucket_dir, bucket, EventName.added, file_obj)

    return None


async def handle_modified_event(
    src: Optional[Dict[str, Any]],
    bucket_dir: str,
    bucket: str,
    cid: str,
) -> Optional[Event]:
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
            LOG.warning(f'[WEBSOCKET ] Rejected path traversal attempt: "{dir_obj.path}"')
            return None
        if dir_obj.path and dir_obj.permissions:
            LOG.info(f'[WEBSOCKET ] Modified dir "{dir_obj.path}"')
            await set_permissions(dir_obj.path, dir_obj.permissions)
            return Event(cid, bucket_dir, bucket, EventName.modified, dir_obj)

    # Handle file modification notification
    elif "filepath" in src:
        file_obj = File().from_dict(src, bucket_dir, False)
        # Validate path to prevent traversal attacks
        if file_obj.filepath is None or not is_path_within_bucket(file_obj.filepath, bucket_dir):
            LOG.warning(f'[WEBSOCKET ] Rejected path traversal attempt: "{file_obj.filepath}"')
            return None
        LOG.info(f'[WEBSOCKET ] Modified file "{file_obj.filepath}"')
        return Event(cid, bucket_dir, bucket, EventName.modified, file_obj)

    return None


async def handle_deleted_event(
    src: Optional[Dict[str, Any]],
    bucket_dir: str,
    bucket: str,
    cid: str,
) -> Optional[Event]:
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
        # Validate path to prevent traversal attacks
        if dir_obj.path is None or not is_path_within_bucket(dir_obj.path, bucket_dir):
            LOG.warning(f'[WEBSOCKET ] Rejected path traversal attempt: "{dir_obj.path}"')
            return None
        if dir_obj.path:
            if not exists(dir_obj.path):
                LOG.warning(f'[WEBSOCKET ] Dir: "{dir_obj.path}" not found.')
            else:
                LOG.info(f'[WEBSOCKET ] Deleted dir "{dir_obj.path}"')
                rmtree(dir_obj.path, ignore_errors=False)
            return Event(cid, bucket_dir, bucket, EventName.deleted, dir_obj)

    # Handle file deletion
    elif "filepath" in src:
        file_obj = File().from_dict(src, bucket_dir, False)
        # Validate path to prevent traversal attacks
        if file_obj.filepath is None or not is_path_within_bucket(file_obj.filepath, bucket_dir):
            LOG.warning(f'[WEBSOCKET ] Rejected path traversal attempt: "{file_obj.filepath}"')
            return None
        if file_obj.filepath:
            if not exists(file_obj.filepath):
                LOG.warning(f'[WEBSOCKET ] File: "{file_obj.filepath}" not found.')
            else:
                LOG.info(f'[WEBSOCKET ] Deleted file "{file_obj.filepath}"')
                remove(file_obj.filepath)
            # Remove from hash cache
            del_md5_hash(file_obj.filepath)
            return Event(cid, bucket_dir, bucket, EventName.deleted, file_obj)

    return None


async def handle_renamed_event(
    src: Optional[Dict[str, Any]],
    dst: Optional[Dict[str, Any]],
    bucket_dir: str,
    bucket: str,
    cid: str,
) -> Optional[Event]:
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
            LOG.warning(f'[WEBSOCKET ] Rejected path traversal attempt: "{src_dir.path}"')
            return None
        if dst_dir.path is None or not is_path_within_bucket(dst_dir.path, bucket_dir):
            LOG.warning(f'[WEBSOCKET ] Rejected path traversal attempt: "{dst_dir.path}"')
            return None
        if src_dir.path and dst_dir.path:
            LOG.info(f'[WEBSOCKET ] Renamed "{src_dir.path}" -> "{dst_dir.path}"')
            rename(src_dir.path, dst_dir.path)
            return Event(cid, bucket_dir, bucket, EventName.renamed, src_dir, dst_dir)

    # Handle file rename
    elif src.get("filepath"):
        src_file = File().from_dict(src, bucket_dir, False)
        dst_file = File().from_dict(dst, bucket_dir, False)
        # Validate both paths to prevent traversal attacks
        if src_file.filepath is None or not is_path_within_bucket(src_file.filepath, bucket_dir):
            LOG.warning(f'[WEBSOCKET ] Rejected path traversal attempt: "{src_file.filepath}"')
            return None
        if dst_file.filepath is None or not is_path_within_bucket(dst_file.filepath, bucket_dir):
            LOG.warning(f'[WEBSOCKET ] Rejected path traversal attempt: "{dst_file.filepath}"')
            return None
        if src_file.filepath and dst_file.filepath:
            LOG.info(f'[WEBSOCKET ] Renamed "{src_file.filepath}" -> "{dst_file.filepath}"')
            rename(src_file.filepath, dst_file.filepath)
            return Event(cid, bucket_dir, bucket, EventName.renamed, src_file, dst_file)

    return None


async def handle_symlink_event(
    src: Optional[Dict[str, Any]],
    bucket_dir: str,
    bucket: str,
    cid: str,
) -> Optional[Event]:
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
                LOG.warning(f'[WEBSOCKET ] Rejected path traversal attempt: "{link.filepath}"')
                return None

            # Validate symlink target to prevent path traversal attacks
            if not is_safe_symlink_target(link.filepath, target, bucket_dir):
                LOG.warning(
                    f'[WEBSOCKET ] Rejected unsafe symlink "{link.filepath}" -> "{target}" '
                    f"(target escapes bucket directory)"
                )
                return None

            LOG.info(f'[WEBSOCKET ] Symlink "{link.filepath}" -> "{target}"')
            # Ensure parent directory exists
            parent_dir = dirname(link.filepath)
            if not exists(parent_dir):
                makedirs(parent_dir, exist_ok=True)

            # Remove existing symlink or file if present
            if islink(link.filepath) or exists(link.filepath):
                unlink(link.filepath)

            # Create the symlink
            os_symlink(target, link.filepath)
            return Event(cid, bucket_dir, bucket, EventName.symlink, link)

    return None

