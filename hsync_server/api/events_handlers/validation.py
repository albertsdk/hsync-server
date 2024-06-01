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


def is_path_within_bucket(path: str, bucket_dir: str) -> bool:
    """
    Validate that a path is within the bucket directory.

    Prevents path traversal attacks where a malicious client could
    access files outside the bucket (e.g., /etc/passwd).

    Args:
        path: Full path to validate.
        bucket_dir: The bucket directory that should contain all files.

    Returns:
        True if the path is within the bucket, False otherwise.
    """
    if not path:
        return False
    bucket_path = Path(bucket_dir).resolve()
    target_path = Path(path).resolve()
    try:
        target_path.relative_to(bucket_path)
        return True
    except ValueError:
        return False


def is_safe_symlink_target(symlink_path: str, target: str, bucket_dir: str) -> bool:
    """
    Validate that a symlink target is safe and within the bucket directory.

    Prevents symlink attacks where a malicious client could create symlinks
    pointing to sensitive files outside the bucket (e.g., /etc/passwd).

    Args:
        symlink_path: Full path where the symlink will be created.
        target: The target path the symlink will point to.
        bucket_dir: The bucket directory that should contain all files.

    Returns:
        True if the target is safe (within bucket), False otherwise.
    """
    bucket_path = Path(bucket_dir).resolve()

    # If target is absolute, it must be within the bucket
    if Path(target).is_absolute():
        target_resolved = Path(target).resolve()
    else:
        # For relative targets, resolve from the symlink's parent directory
        symlink_parent = Path(symlink_path).parent
        target_resolved = (symlink_parent / target).resolve()

    # Check if the resolved target is within the bucket directory
    try:
        target_resolved.relative_to(bucket_path)
        return True
    except ValueError:
        return False
