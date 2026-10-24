# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""
File utility functions for HSYNC Server.

This module provides utility functions for file operations:
- Path safety validation (prevent traversal attacks)
- Timestamp updates
"""

from os import utime
from pathlib import Path

# Receiver-side far-future mtime clamp (conflict engineering); the SSOT value
# and rationale live in ``hsync_common.constants``.
from hsync_common.constants import (
    CORRECTED_MTIME_MAX_FUTURE_SKEW as MTIME_MAX_FUTURE_SKEW_SECONDS,
)
from hsync_common.constants import HSYNCKEEP_FILENAME
from hsync_common.models.sentinel import (
    ensure_sentinel,
    is_sentinel_file,  # noqa: F401  (re-exported for server call sites)
    remove_empty_dir,
)
from hsync_common.time_utils import to_epoch, utc_now_epoch
from loguru import logger as log


def ensure_sentinel_in_dir(dir_path: str) -> None:
    """Create a .hsynckeep sentinel if *dir_path* has no syncable entry.

    After a file is deleted the parent directory may become empty; the marker
    preserves the directory structure during sync. The rule itself is shared
    (``hsync_common.models.sentinel``) so the P2P master behaves identically.
    """
    if not Path(dir_path).is_dir():
        return
    if ensure_sentinel(dir_path):
        log.debug(f'Created sentinel "{Path(dir_path) / HSYNCKEEP_FILENAME}"')


def cleanup_empty_dir(dir_path: str, bucket_root: str | None = None) -> None:
    """Remove *dir_path* if it is completely empty (no entries at all).

    Called after deleting a sentinel file so the directory does not linger on
    disk. The bucket root is never removed, even when empty: a peer deleting the
    root sentinel must not destroy the sync target.
    """
    if remove_empty_dir(dir_path, keep_root=bucket_root):
        log.debug(f'Removed empty directory "{dir_path}"')


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
