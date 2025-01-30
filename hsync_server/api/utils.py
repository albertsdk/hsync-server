# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
API utility functions for HSYNC Server.

This module provides common utility functions used across API endpoints,
including path validation, SHA-256 computation, and file permission handling.
"""

from asyncio import to_thread
from hashlib import sha256
from os import chmod

import aiofiles
from hsync_common.permissions import get_permissions as _read_permissions
from hsync_common.permissions import octal_to_mode
from loguru import logger as log

from hsync_server.config import get_buckets_path
from hsync_server.models.dir import Dir
from hsync_server.path_validation import is_valid_path as validate_path_traversal

OWNER_DIRECTORY_ACCESS = 0o700


def is_valid_bucket(value: str) -> bool:
    """
    Validate a bucket name.

    Bucket names must contain only alphanumeric characters, hyphens
    and underscores. They cannot start or end with a hyphen or underscore.

    Args:
        value: The bucket name to validate.

    Returns:
        True if the bucket name is valid, False otherwise.
    """
    import re

    return bool(re.match(r"^[a-zA-Z0-9]([a-zA-Z0-9_-]*[a-zA-Z0-9])?$", value))


def is_valid_dir(target_path: str) -> bool:
    """
    Validate a directory path to prevent path traversal attacks.

    Ensures the target path resolves to a location within the
    buckets directory, preventing access to files outside
    the allowed storage area.

    Args:
        target_path: Relative path to validate.

    Returns:
        True if the path is safe, False if it escapes the bucket root.
    """
    # Use the new robust is_valid_path function
    return validate_path_traversal(target_path, get_buckets_path())


async def compute_sha256(filepath: str, chunk_size: int = 8192) -> str:
    """
    Asynchronously compute the SHA-256 checksum of a file.

    Reads the file in chunks to handle large files efficiently
    without loading the entire file into memory.

    Args:
        filepath: Path to the file.
        chunk_size: Size of chunks to read (default 8KB).

    Returns:
        Hexadecimal SHA-256 digest string.
    """
    hash_sha256 = sha256()
    async with aiofiles.open(filepath, "rb") as f:
        while chunk := await f.read(chunk_size):
            hash_sha256.update(chunk)
    return hash_sha256.hexdigest()


async def get_permissions(obj_path: str) -> str:
    """
    Get the file permissions as a canonical octal string.

    Args:
        obj_path: Path to the file or directory.

    Returns:
        Octal permission string (e.g., '644').
    """
    return await to_thread(_read_permissions, obj_path)


async def set_permissions(file_path: str, permissions: str) -> None:
    """
    Set file permissions from a canonical octal string.

    Parses an octal permission string (e.g., '664') and applies the
    corresponding numeric mode to the file.

    Args:
        file_path: Path to the file.
        permissions: Octal permission string.
    """
    log.debug(f'Set permissions: "{file_path}" ({permissions})')

    mode = octal_to_mode(permissions)
    await to_thread(chmod, file_path, mode)


async def set_directory_permissions(dir_path: str, permissions: str) -> None:
    """Set directory permissions without making bucket storage inaccessible.

    Directory permissions come from clients as an octal string (for example
    ``755``).  The server must always keep owner rwx access on bucket
    directories so that it can traverse, update and back up its own storage
    tree.
    """
    mode = octal_to_mode(permissions) | OWNER_DIRECTORY_ACCESS
    log.debug(f'Set directory permissions: "{dir_path}" ({permissions} -> {mode:o})')
    await to_thread(chmod, dir_path, mode)


async def get_dir_info(path: str) -> Dir:
    """
    Get directory information including permissions.

    Args:
        path: Path to the directory.

    Returns:
        Dir object containing path and permissions.
    """
    permissions = await get_permissions(path)
    return Dir(
        path,
        permissions,
    )
