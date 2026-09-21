# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
Path validation utilities for WebSocket event handlers.

This module provides functions to validate paths and symlink targets
to prevent path traversal and symlink attacks.
"""

from pathlib import Path


def is_path_within_bucket(path: str, bucket_dir: str, allow_root: bool = True) -> bool:
    """
    Validate that a path is within the bucket directory.

    Prevents path traversal attacks where a malicious client could
    access files outside the bucket (e.g., /etc/passwd).

    Args:
        path: Full path to validate.
        bucket_dir: The bucket directory that should contain all files.
        allow_root: When False, the bucket directory itself is rejected.
            Use this for destructive operations (e.g. rmtree on a directory
            delete event) to ensure a client cannot wipe the entire bucket
            by issuing a delete on the root path.

    Returns:
        True if the path is within the bucket, False otherwise.
    """
    if not path:
        return False
    bucket_path = Path(bucket_dir).resolve()
    target_path = Path(path).resolve()
    try:
        target_path.relative_to(bucket_path)
    except ValueError:
        return False
    return allow_root or target_path != bucket_path
