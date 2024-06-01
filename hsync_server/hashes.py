# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
File hash management module for HSYNC Server.

This module provides an in-memory cache for MD5 file checksums.
File paths are hashed with SHA256 for privacy/security, and the
corresponding MD5 checksums are stored for quick duplicate detection.
"""

import hashlib
from os import walk
from os.path import isdir, join

from hsync_server.models.dir import is_hidden_dir
from hsync_server.models.file import is_hidden_file

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


def get_md5_hash(filepath: str) -> str | None:
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
        # Skip hidden directories using is_hidden_dir
        dirs[:] = [d for d in dirs if not is_hidden_dir(f"{root}/{d}")]

        for file in files:
            file_path = join(root, file)
            # Skip hidden files using is_hidden_file
            if not is_hidden_file(file_path):
                md5 = _compute_md5(file_path)
                set_md5_hash(file_path, md5)
                file_count += 1

    return file_count
