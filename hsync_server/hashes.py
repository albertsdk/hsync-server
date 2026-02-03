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
File hash management module for HSYNC Server.

This module provides an in-memory cache for MD5 file checksums.
File paths are hashed with SHA256 for privacy/security, and the
corresponding MD5 checksums are stored for quick duplicate detection.
"""

import hashlib

from os import walk
from os.path import isdir, join
from typing import Optional


# In-memory storage for file hashes
# Each entry is a dict mapping SHA256(filepath) -> MD5(file_content)
FILE_HASHES: list[dict[str, str]] = []


def set_md5_hash(filepath: str, md5sum: str) -> None:
    """
    Store an MD5 hash for a file path.

    The file path is hashed with SHA256 before storage for privacy.

    Args:
        filepath: Absolute path to the file.
        md5sum: MD5 checksum of the file content.
    """
    hashed_key = hashlib.new("sha256", filepath.encode()).hexdigest()
    FILE_HASHES.append({hashed_key: md5sum})


def get_md5_hash(filepath: str) -> Optional[str]:
    """
    Retrieve the stored MD5 hash for a file path.

    Args:
        filepath: Absolute path to the file.

    Returns:
        The MD5 checksum if found, None otherwise.
    """
    hashed_key = hashlib.new("sha256", filepath.encode()).hexdigest()
    for entry in FILE_HASHES:
        if hashed_key in entry:
            return entry[hashed_key]
    return None


def del_md5_hash(filepath: str) -> bool:
    """
    Delete the stored MD5 hash for a file path.

    Args:
        filepath: Absolute path to the file.

    Returns:
        True if the hash was found and deleted, False otherwise.
    """
    hashed_key = hashlib.new("sha256", filepath.encode()).hexdigest()
    for entry in FILE_HASHES:
        if hashed_key in entry:
            FILE_HASHES.remove(entry)
            return True
    return False


def set_md5_hashes(directory: str) -> int:
    """
    Compute and store MD5 hashes for all files in a directory.

    Recursively walks the directory tree, computing MD5 checksums
    for all non-hidden files. Hidden files and directories (starting
    with '.') are skipped.

    Args:
        directory: Root directory to scan for files.

    Returns:
        Number of files processed and stored.

    Raises:
        ValueError: If the directory path is invalid.
    """
    global FILE_HASHES

    def _compute_md5(filepath: str, chunk_size: int = 8192) -> str:
        """
        Compute MD5 checksum of a file using chunked reading.

        Args:
            filepath: Path to the file.
            chunk_size: Size of chunks to read (default 8KB).

        Returns:
            Hexadecimal MD5 digest string.
        """
        hasher = hashlib.md5()
        with open(filepath, "rb") as file:
            for chunk in iter(lambda: file.read(chunk_size), b""):
                hasher.update(chunk)
        return hasher.hexdigest()

    if not isdir(directory):
        raise ValueError(f"Invalid directory path: {directory}")

    # Reset the hash storage
    FILE_HASHES = []
    file_count = 0

    # Walk directory tree, skipping hidden directories
    for root, dirs, files in walk(directory):
        dirs[:] = [d for d in dirs if not str(d).startswith(".")]

        for file in files:
            # Skip hidden files
            if not str(file).startswith("."):
                file_path = join(root, file)
                md5 = _compute_md5(file_path)
                set_md5_hash(file_path, md5)
                file_count += 1

    return file_count
