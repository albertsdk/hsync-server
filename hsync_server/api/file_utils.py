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

from datetime import datetime, timezone
from loguru import logger as LOG
from os import utime
from re import match


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
    Update file access and modification timestamps.

    Accepts timestamps as either Unix timestamps (digit strings) or
    ISO format datetime strings.

    Args:
        file_location: Path to the file.
        ctime: Creation/access time (Unix timestamp or ISO string).
        mtime: Modification time (Unix timestamp or ISO string).
    """
    # Only update if at least one timestamp is provided
    if ctime or mtime:
        # Parse ctime (can be Unix timestamp or ISO format)
        if ctime and ctime.isdigit():
            _ctime = int(
                datetime.fromtimestamp(int(ctime), timezone.utc).timestamp()
            )
        elif ctime:
            _ctime = int(datetime.fromisoformat(ctime).timestamp())
        else:
            _ctime = None

        # Parse mtime (can be Unix timestamp or ISO format)
        if mtime and mtime.isdigit():
            _mtime = int(
                datetime.fromtimestamp(int(mtime), timezone.utc).timestamp()
            )
        elif mtime:
            _mtime = int(datetime.fromisoformat(mtime).timestamp())
        else:
            _mtime = None

        LOG.debug(f"Update timestamp: {file_location} ({ctime}, {mtime})")
        # Set access time and modification time (fallback to the other if one is None)
        # Only call utime if we have at least one valid timestamp
        atime = _ctime or _mtime
        mtime_val = _mtime or _ctime
        if atime is not None and mtime_val is not None:
            utime(file_location, (atime, mtime_val))

