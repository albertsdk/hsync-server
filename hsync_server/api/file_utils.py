# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""
File utility functions for HSYNC Server.

This module provides utility functions for file operations:
- Path safety validation (prevent traversal attacks)
- Timestamp updates
"""

from os import listdir, rmdir, utime
from pathlib import Path

from hsync_common.time_utils import to_epoch, utc_now_epoch
from loguru import logger as log

# Wire-supplied mtimes may not exceed now + this skew: a peer asserting
# far-future timestamps would win every mtime-based conflict resolution on
# every other peer (conflict engineering).
MTIME_MAX_FUTURE_SKEW_SECONDS = 300


def is_sentinel_file(filepath: str) -> bool:
    """Return True if *filepath* is a .hsynckeep sentinel file."""
    from hsync_server.constants import HSYNCKEEP_FILENAME

    return Path(filepath).name == HSYNCKEEP_FILENAME


def ensure_sentinel_in_dir(dir_path: str) -> None:
    """Create a .hsynckeep sentinel if *dir_path* is an empty directory.

    After a file is deleted the parent directory may become empty.
    Placing a sentinel preserves the directory structure during sync.

    A directory is considered "empty" when it contains no syncable
    (non-hidden) files.  An already-existing sentinel counts as
    syncable, so a second one is never created.

    Args:
        dir_path: Absolute path to the directory to check.
    """
    from hsync_common.models.file import is_hidden_file

    from hsync_server.constants import HSYNCKEEP_FILENAME

    if not Path(dir_path).exists() or not Path(dir_path).is_dir():
        return

    try:
        entries = listdir(dir_path)
    except OSError:
        return

    has_syncable = any(not is_hidden_file(e) for e in entries)
    if has_syncable:
        return

    sentinel = str(Path(dir_path) / HSYNCKEEP_FILENAME)
    if not Path(sentinel).exists():
        try:
            open(sentinel, "w").close()
            log.debug(f'Created sentinel "{sentinel}"')
        except OSError as exc:
            log.warning(f'Cannot create sentinel "{sentinel}": {exc}')


def cleanup_empty_dir(dir_path: str, bucket_root: str | None = None) -> None:
    """Remove *dir_path* if it is completely empty (no files at all).

    Called after deleting a sentinel file so that the directory does
    not linger on disk once it has no real content.  Only removes the
    directory if it contains zero entries (hidden or otherwise).

    The bucket root directory is never removed, even when empty, to
    prevent a malicious or legacy client from destroying a bucket by
    deleting the ``.hsynckeep`` sentinel located at its root.

    Args:
        dir_path: Absolute path to the directory to check.
        bucket_root: Absolute path of the bucket root that must NOT be
            removed. When omitted no root guard is applied (legacy
            behaviour, used by tests that operate outside of a bucket).
    """
    if not Path(dir_path).exists() or not Path(dir_path).is_dir():
        return

    if bucket_root and Path(dir_path).resolve() == Path(bucket_root).resolve():
        return

    try:
        entries = listdir(dir_path)
    except OSError:
        return

    if entries:
        return

    try:
        rmdir(dir_path)
        log.debug(f'Removed empty directory "{dir_path}"')
    except OSError as exc:
        log.warning(f'Cannot remove directory "{dir_path}": {exc}')


def update_file_timestamp(file_location: str, mtime: str | None = None) -> None:
    """
    Restore the file modification time.

    Accepts the timestamp as either a Unix timestamp (digit string) or an
    ISO format datetime string. Only the modification time is restored: POSIX
    ``ctime`` is not settable and writing it into ``atime`` would corrupt the
    access time, so the access time is set to ``mtime`` to keep them consistent.
    The wire ``ctime`` field sent by clients is accepted upstream but never
    applied here (POSIX cannot set it).

    Args:
        file_location: Path to the file.
        mtime: Modification time (Unix timestamp or ISO string).
    """
    if not mtime:
        return

    # Parse mtime (can be Unix timestamp or ISO format)
    _mtime = to_epoch(mtime)
    if _mtime is None:
        return

    # Clamp far-future peer-supplied timestamps (conflict engineering would
    # favour the newest mtime on every peer).
    max_ts = utc_now_epoch() + MTIME_MAX_FUTURE_SKEW_SECONDS
    if _mtime > max_ts:
        log.warning(f"Clamping far-future mtime ({_mtime} -> {max_ts}): {file_location}")
        _mtime = max_ts

    log.debug(f"Update timestamp: {file_location} (mtime={mtime})")
    # Set both access and modification time to mtime; ctime is not preservable.
    utime(file_location, (_mtime, _mtime))
