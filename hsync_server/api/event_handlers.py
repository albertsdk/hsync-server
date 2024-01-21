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
WebSocket event handlers for HSYNC Server.

This module provides handler functions for different event types:
- handle_added_event: Create new files/directories
- handle_modified_event: Update existing files/directories
- handle_deleted_event: Remove files/directories
- handle_renamed_event: Rename/move files or directories
- handle_symlink_event: Create or update symbolic links
"""

import logging

from os import makedirs, rename, remove, symlink as os_symlink, unlink
from os.path import dirname, exists, islink
from shutil import rmtree
from typing import Any, Dict, Optional

from hsync_server.api.utils import set_permissions
from hsync_server.hashes import del_md5_hash
from hsync_server.models.dir import Dir
from hsync_server.models.event import Event, EventName
from hsync_server.models.file import File
from hsync_server.models.symlink import Symlink


# Logger instance for this module
LOG = logging.getLogger(__name__)


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
        if dir_obj.path and dir_obj.permissions:
            LOG.info(f'[WEBSOCKET ] Added dir "{dir_obj.path}"')
            makedirs(dir_obj.path, exist_ok=True)
            await set_permissions(dir_obj.path, dir_obj.permissions)
            return Event(cid, bucket_dir, bucket, EventName.added, dir_obj)

    # Handle file creation notification
    elif "filepath" in src:
        file_obj = File().from_dict(src, bucket_dir)
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
        if dir_obj.path and dir_obj.permissions:
            LOG.info(f'[WEBSOCKET ] Modified dir "{dir_obj.path}"')
            await set_permissions(dir_obj.path, dir_obj.permissions)
            return Event(cid, bucket_dir, bucket, EventName.modified, dir_obj)

    # Handle file modification notification
    elif "filepath" in src:
        file_obj = File().from_dict(src, bucket_dir)
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
        if src_dir.path and dst_dir.path:
            LOG.info(f'[WEBSOCKET ] Renamed "{src_dir.path}" -> "{dst_dir.path}"')
            rename(src_dir.path, dst_dir.path)
            return Event(cid, bucket_dir, bucket, EventName.renamed, src_dir, dst_dir)

    # Handle file rename
    elif src.get("filepath"):
        src_file = File().from_dict(src, bucket_dir, False)
        dst_file = File().from_dict(dst, bucket_dir, False)
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

