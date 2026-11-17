# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""
File hash management module for HSYNC Server.

This module provides an in-memory cache for SHA-256 file checksums.
File paths are hashed with SHA-256 for privacy/security, and the
corresponding SHA-256 checksums are stored for quick duplicate detection.

The cache is a bounded LRU (``FILE_HASHES_MAX_SIZE``): it is keyed by every
path the server lists or accepts, so an unbounded map would grow with the whole
file population of the host (one entry per file, ~360 bytes each measured).
"""

import hashlib
from os import stat as os_stat
from os import walk
from pathlib import Path

from hsync_common.hashing import compute_sha256, compute_sha256_async
from hsync_common.models.dir import is_hidden_dir
from hsync_common.models.file import is_hidden_file, set_sha256_cache_provider

# In-memory storage for file hashes
# Maps SHA256(filepath) -> (SHA256(file_content), mtime_ns, size), ordered
# least-recently-used first. A plain dict is used (insertion-ordered) rather
# than OrderedDict, so the map stays a drop-in for callers/tests that rebind it.
FILE_HASHES: dict[str, tuple[str, int, int]] = {}

# Upper bound on retained entries. Without it the map grew with every distinct
# file ever listed or uploaded (~360 B/entry), i.e. hundreds of MB on a large
# bucket. Past the cap, the least-recently-used entry is evicted (a fresh/moved
# file is only ever a cache miss, never an incorrect result).
FILE_HASHES_MAX_SIZE: int = 100_000


def _hashed_path_key(filepath: str) -> str:
    """Return the cache key for *filepath* (SHA-256 of the path, privacy)."""
    return hashlib.sha256(filepath.encode()).hexdigest()


def _cache_sha256_hash(hashed_key: str, entry: tuple[str, int, int]) -> None:
    """Insert/refresh *entry* as most-recently-used, evicting the LRU at the cap."""
    # Re-insert so the key becomes most-recently-used: a plain dict assignment
    # on an existing key would keep its old (staler) position.
    FILE_HASHES.pop(hashed_key, None)
    FILE_HASHES[hashed_key] = entry
    if len(FILE_HASHES) > FILE_HASHES_MAX_SIZE:
        del FILE_HASHES[next(iter(FILE_HASHES))]  # evict least-recently-used


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
    _cache_sha256_hash(_hashed_path_key(filepath), (sha256sum, st.st_mtime_ns, st.st_size))


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
    hashed_key: str = _hashed_path_key(filepath)
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
    # Fresh hit: mark as most-recently-used so it survives LRU eviction.
    FILE_HASHES.pop(hashed_key, None)
    FILE_HASHES[hashed_key] = entry
    return sha256sum


def del_sha256_hash(filepath: str) -> bool:
    """
    Delete the stored SHA-256 hash for a file path.

    Args:
        filepath: Absolute path to the file.

    Returns:
        True if the hash was found and deleted, False otherwise.
    """
    hashed_key: str = _hashed_path_key(filepath)
    if hashed_key in FILE_HASHES:
        del FILE_HASHES[hashed_key]
        return True
    return False


def register_sha256_cache_provider() -> None:
    """Route hsync-common's ``File.get`` cache through the preloaded server cache.

    Consumer: ``hsync_server.main`` lifespan, before ``set_sha256_hashes``. The
    listing (``api/files/list.py``) builds each entry with ``File.get``, so
    without this bridge it hashed files into hsync-common's 10k LRU instead of
    the cache the preload fills (100k): the first listing after every start, and
    every listing past 10k files, re-read whole files. Tests install and reset it
    explicitly.
    """
    set_sha256_cache_provider(
        _provider_get_sha256,
        _provider_set_sha256,
        _provider_invalidate_sha256,
        _provider_clear_sha256,
    )


def _provider_get_sha256(filepath: str, mtime_ns: int, ctime_ns: int, size: int) -> str | None:
    """Serve a fresh preloaded digest to ``File.get`` (ctime is not part of the key)."""
    hashed_key = _hashed_path_key(filepath)
    entry = FILE_HASHES.get(hashed_key)
    if entry is None:
        return None
    sha256sum, cached_mtime_ns, cached_size = entry
    if (cached_mtime_ns, cached_size) != (mtime_ns, size):
        FILE_HASHES.pop(hashed_key, None)
        return None
    FILE_HASHES.pop(hashed_key, None)
    FILE_HASHES[hashed_key] = entry  # touch: survive LRU eviction
    return sha256sum


def _provider_set_sha256(
    filepath: str, sha256sum: str, mtime_ns: int, ctime_ns: int, size: int
) -> None:
    """Store a ``File.get`` miss in the server cache with its stat identity."""
    _cache_sha256_hash(_hashed_path_key(filepath), (sha256sum, mtime_ns, size))


def _provider_invalidate_sha256(filepath: str) -> None:
    """Drop one path from the server cache (rename/delete/upload invalidation)."""
    FILE_HASHES.pop(_hashed_path_key(filepath), None)


def _provider_clear_sha256() -> None:
    """Clear the server cache (hsync-common's ``clear_sha256_cache`` routes here)."""
    FILE_HASHES.clear()


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
