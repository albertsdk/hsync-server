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
from os import stat as os_stat
from os import walk
from pathlib import Path

from hsync_common.hashing import compute_sha256, compute_sha256_async
from hsync_common.models.dir import is_hidden_dir
from hsync_common.models.file import is_hidden_file

# In-memory storage for file hashes
# Maps SHA256(filepath) -> (SHA256(file_content), mtime_ns, size)
FILE_HASHES: dict[str, tuple[str, int, int]] = {}


def set_sha256_hash(filepath: str, sha256sum: str) -> None:
    """
    Store a SHA-256 hash for a file path, with its current metadata.

    The digest is stored together with the (mtime_ns, size) observed now, so
    :func:`get_sha256_hash` can refuse stale entries instead of serving a
    checksum that no longer describes the file on disk. A missing/unreadable
    file is not cached (there is nothing verifiable to attach the digest to).

    The file path is hashed with SHA-256 before storage for privacy.

    Args:
        filepath: Absolute path to the file.
        sha256sum: SHA-256 checksum of the file content.
    """
    try:
        st = os_stat(filepath)
    except OSError:
        return
    hashed_key: str = hashlib.sha256(filepath.encode()).hexdigest()
    FILE_HASHES[hashed_key] = (sha256sum, st.st_mtime_ns, st.st_size)


def get_sha256_hash(filepath: str) -> str | None:
    """
    Retrieve the stored SHA-256 hash for a file path if it is still fresh.

    The cached digest is served only while the file's current (mtime_ns,
    size) match the values captured at caching time; a changed or vanished
    file returns ``None`` and the stale entry is dropped, so callers
    recompute instead of trusting an outdated checksum.

    Args:
        filepath: Absolute path to the file.

    Returns:
        The SHA-256 checksum if found and fresh, None otherwise.
    """
    hashed_key: str = hashlib.sha256(filepath.encode()).hexdigest()
    entry = FILE_HASHES.get(hashed_key)
    if entry is None:
        return None
    sha256sum, mtime_ns, size = entry
    try:
        st = os_stat(filepath)
    except OSError:
        del FILE_HASHES[hashed_key]
        return None
    if (st.st_mtime_ns, st.st_size) != (mtime_ns, size):
        del FILE_HASHES[hashed_key]
        return None
    return sha256sum


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


async def get_or_compute_sha256(filepath: str) -> str:
    """Return the fresh cached SHA-256 for *filepath*, recomputing it otherwise.

    Serves a cache entry only while it is still fresh; on a miss (or stale
    entry) the digest is recomputed asynchronously. Shared by the delta
    endpoints (signatures, apply) so the cache-or-recompute fallback lives in
    one place.
    """
    return get_sha256_hash(filepath) or await compute_sha256_async(filepath)


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
    if not Path(directory).is_dir():
        raise ValueError(f"Invalid directory path: {directory}")

    # Reset the hash storage
    FILE_HASHES.clear()
    file_count = 0

    # Walk directory tree, skipping hidden directories
    for root, dirs, files in walk(directory):
        # Skip hidden directories using is_hidden_dir
        dirs[:] = [d for d in dirs if not is_hidden_dir(f"{root}/{d}")]

        for file in files:
            file_path = f"{root}/{file}"  # string concat: no Path object per entry
            # Skip hidden files using is_hidden_file
            if not is_hidden_file(file_path):
                sha256 = compute_sha256(file_path)
                set_sha256_hash(file_path, sha256)
                file_count += 1

    return file_count
