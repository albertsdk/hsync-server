"""
API utility functions for HSYNC Server.

This module provides common utility functions used across API endpoints,
including path validation, MD5 computation, and file permission handling.
"""

import aiofiles
import stat

from hsync_server.config import HSYNC_BUCKETS
from hsync_server.models.dir import Dir
from asyncio import to_thread
from hashlib import md5
from logging import getLogger
from os import chmod, stat as os_stat
from pathlib import Path


# Logger instance for this module
LOG = getLogger("main")


def is_valid_bucket(value: str) -> bool:
    """
    Validate a bucket name.

    Bucket names must contain only alphanumeric characters and hyphens.
    Hyphens cannot be at the beginning or end.

    Args:
        value: The bucket name to validate.

    Returns:
        True if the bucket name is valid, False otherwise.
    """
    import re
    return bool(re.match(r"^[a-zA-Z0-9][a-zA-Z0-9-]*[a-zA-Z0-9]$|^[a-zA-Z0-9]$", value))


def is_valid_dir(target_path: str) -> bool:
    """
    Validate a directory path to prevent path traversal attacks.

    Ensures the target path resolves to a location within the
    HSYNC_BUCKETS directory, preventing access to files outside
    the allowed storage area.

    Args:
        target_path: Relative path to validate.

    Returns:
        True if the path is safe, False if it escapes the bucket root.
    """
    reference = Path(HSYNC_BUCKETS).resolve()
    target = Path(f"{HSYNC_BUCKETS}/{target_path}").resolve()
    try:
        target.relative_to(reference)
        return True
    except ValueError:
        return False


async def compute_md5(filepath: str, chunk_size: int = 8192) -> str:
    """
    Asynchronously compute the MD5 checksum of a file.

    Reads the file in chunks to handle large files efficiently
    without loading the entire file into memory.

    Args:
        filepath: Path to the file.
        chunk_size: Size of chunks to read (default 8KB).

    Returns:
        Hexadecimal MD5 digest string.
    """
    hash_md5 = md5()
    async with aiofiles.open(filepath, "rb") as f:
        while chunk := await f.read(chunk_size):
            hash_md5.update(chunk)
    return hash_md5.hexdigest()


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
    LOG.debug(f'Set permissions: "{file_path}" ({permissions})')

    # Convert symbolic permissions to numeric mode
    mode = 0
    permission_types = {"r": 4, "w": 2, "x": 1}

    # Process owner, group, and other permission sections
    for i, section in enumerate(
        [permissions[1:4], permissions[4:7], permissions[7:10]]
    ):
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
