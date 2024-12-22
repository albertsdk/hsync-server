# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
File utility functions for HSYNC Server.

This module provides utility functions for file operations:
- Path safety validation (prevent traversal attacks)
- Permission format validation
- Timestamp updates
"""

from datetime import UTC, datetime
from os import listdir, rmdir, utime
from pathlib import Path
from re import match

from loguru import logger as log


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
    from hsync_server.constants import HSYNCKEEP_FILENAME
    from hsync_server.models.file import is_hidden_file

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
            open(sentinel, "w").close()  # noqa: SIM115
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


def is_valid_permission_format(permissions: str) -> bool:
    """
    Validate a symbolic permission string format.

    Args:
        permissions: Permission string to validate (e.g., '-rw-r--r--', 'drwxr-xr-x').

    Returns:
        True if the format is valid, False otherwise.
    """
    # First character: file type (d=dir, l=link, -=file, b=block, c=char, p=pipe, s=socket)
    return bool(match(r"^[dlbcps\-][rwx-]{9}$", permissions))


def update_file_timestamp(
    file_location: str, ctime: str | None = None, mtime: str | None = None
) -> None:
    """
    Restore the file modification time.

    Accepts the timestamp as either a Unix timestamp (digit string) or an
    ISO format datetime string. Only the modification time is restored: POSIX
    ``ctime`` is not settable and writing it into ``atime`` would corrupt the
    access time, so the access time is set to ``mtime`` to keep them consistent.

    Args:
        file_location: Path to the file.
        ctime: Accepted for protocol compatibility but not applied (POSIX
            ctime cannot be set).
        mtime: Modification time (Unix timestamp or ISO string).
    """
    if not mtime:
        return

    # Parse mtime (can be Unix timestamp or ISO format)
    if mtime.isdigit():
        _mtime = int(datetime.fromtimestamp(int(mtime), UTC).timestamp())
    else:
        _mtime = int(datetime.fromisoformat(mtime).timestamp())

    log.debug(f"Update timestamp: {file_location} (mtime={mtime})")
    # Set both access and modification time to mtime; ctime is not preservable.
    utime(file_location, (_mtime, _mtime))
