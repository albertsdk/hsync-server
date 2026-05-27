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

import stat
from asyncio import to_thread
from hashlib import sha256
from os import chmod
from os import stat as os_stat

import aiofiles
from loguru import logger as log

from hsync_server.config import get_buckets_path
from hsync_server.models.dir import Dir
from hsync_server.path_validation import is_valid_path as validate_path_traversal


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
    Get the file permissions in symbolic notation.

    Args:
        obj_path: Path to the file or directory.

    Returns:
        Permission string in symbolic format (e.g., '-rw-r--r--').
    """
    obj_stat = await to_thread(os_stat, obj_path)
    permissions = obj_stat.st_mode
    return stat.filemode(permissions)


async def set_permissions(file_path: str, permissions: str) -> None:
    """
    Set file permissions from symbolic notation.

    Parses a symbolic permission string (e.g., '-rw-rw-r--') and
    applies the corresponding numeric mode to the file.

    Args:
        file_path: Path to the file.
        permissions: Permission string in symbolic format.
    """
    log.debug(f'Set permissions: "{file_path}" ({permissions})')

    # Convert symbolic permissions to numeric mode
    mode = 0
    permission_types = {"r": 4, "w": 2, "x": 1}

    # Process owner, group, and other permission sections
    for i, section in enumerate([permissions[1:4], permissions[4:7], permissions[7:10]]):
        for char in section:
            if char in permission_types:
                mode |= permission_types[char] << (6 - (i * 3))

    await to_thread(chmod, file_path, mode)


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
