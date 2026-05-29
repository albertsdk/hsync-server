# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
File hash management module for HSYNC Server.

This module provides an in-memory cache for SHA-256 file checksums.
File paths are hashed with SHA-256 for privacy/security, and the
corresponding SHA-256 checksums are stored for quick duplicate detection.
"""

import hashlib
from os import walk
from pathlib import Path

from hsync_server.models.dir import is_hidden_dir
from hsync_server.models.file import is_hidden_file

# In-memory storage for file hashes
# Maps SHA256(filepath) -> SHA256(file_content)
FILE_HASHES: dict[str, str] = {}


def set_sha256_hash(filepath: str, sha256sum: str) -> None:
    """
    Store a SHA-256 hash for a file path.

    The file path is hashed with SHA-256 before storage for privacy.

    Args:
        filepath: Absolute path to the file.
        sha256sum: SHA-256 checksum of the file content.
    """
    hashed_key: str = hashlib.sha256(filepath.encode()).hexdigest()
    FILE_HASHES[hashed_key] = sha256sum


def get_sha256_hash(filepath: str) -> str | None:
    """
    Retrieve the stored SHA-256 hash for a file path.

    Args:
        filepath: Absolute path to the file.

    Returns:
        The SHA-256 checksum if found, None otherwise.
    """
    hashed_key: str = hashlib.sha256(filepath.encode()).hexdigest()
    return FILE_HASHES.get(hashed_key)


def del_sha256_hash(filepath: str) -> bool:
    """
    Delete the stored SHA-256 hash for a file path.

    Args:
        filepath: Absolute path to the file.

    Returns:
        True if the hash was found and deleted, False otherwise.
    """
    hashed_key: str = hashlib.sha256(filepath.encode()).hexdigest()
    if hashed_key in FILE_HASHES:
        del FILE_HASHES[hashed_key]
        return True
    return False


def set_sha256_hashes(directory: str) -> int:
    """
    Compute and store SHA-256 hashes for all files in a directory.

    Recursively walks the directory tree, computing SHA-256 checksums
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

    def _compute_sha256(filepath: str, chunk_size: int = 8192) -> str:
        """
        Compute SHA-256 checksum of a file using chunked reading.

        Args:
            filepath: Path to the file.
            chunk_size: Size of chunks to read (default 8KB).

        Returns:
            Hexadecimal SHA-256 digest string.
        """
        hasher = hashlib.sha256()
        with open(filepath, "rb") as file:
            for chunk in iter(lambda: file.read(chunk_size), b""):
                hasher.update(chunk)
        return hasher.hexdigest()

    if not Path(directory).is_dir():
        raise ValueError(f"Invalid directory path: {directory}")

    # Reset the hash storage
    FILE_HASHES = {}
    file_count = 0

    # Walk directory tree, skipping hidden directories
    for root, dirs, files in walk(directory):
        # Skip hidden directories using is_hidden_dir
        dirs[:] = [d for d in dirs if not is_hidden_dir(f"{root}/{d}")]

        for file in files:
            file_path = str(Path(root) / file)
            # Skip hidden files using is_hidden_file
            if not is_hidden_file(file_path):
                sha256 = _compute_sha256(file_path)
                set_sha256_hash(file_path, sha256)
                file_count += 1

    return file_count
