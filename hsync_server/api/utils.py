# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
API utility functions for HSYNC Server.

This module provides common utility functions used across API endpoints,
including path validation, SHA-256 computation, and file permission handling.
"""

from asyncio import to_thread
from os import chmod
from pathlib import Path

from hsync_common.path_validation import (
    is_valid_bucket_name,
    resolve_within,
    validate_remote_relpath,
)
from hsync_common.permissions import (
    OWNER_DIRECTORY_ACCESS,
    octal_to_mode,
    sanitize_remote_permissions,
)
from hsync_common.permissions import get_permissions as _read_permissions
from loguru import logger as log

from hsync_server.config import get_buckets_path


def is_valid_bucket(value: str) -> bool:
    """
    Validate a bucket name.

    Bucket names must contain only alphanumeric characters, hyphens
    and underscores. They cannot start or end with a hyphen or underscore.

    Args:
        value: The bucket name to validate.

    Returns:
        True if the bucket name is valid, False otherwise.
    """
    return is_valid_bucket_name(value)


def resolve_within_bucket(bucket: str, rel: str, *, resolve_final: bool = True) -> str | None:
    """Resolve a client-supplied relative path inside a *specific* bucket.

    The path is validated with
    :func:`hsync_common.path_validation.validate_remote_relpath` (rejecting
    traversal, absolute paths, backslashes, reserved names and internal
    engine artifacts) and confined to ``buckets_path/bucket`` — a path such
    as ``../other-bucket/x`` can never escape into a sibling bucket
    (cross-bucket confinement).

    When *resolve_final* is False the **final component is not dereferenced**:
    a symlink at the endpoint is returned as the link path, so callers that
    must treat symlinks structurally (download refuses them, delete removes
    the link itself, symlink-create replaces the link, delta-apply replaces
    the file) never operate on the target. Intermediate components are still
    fully resolved and confined, so a directory-symlink escaping the bucket
    keeps being rejected.

    Args:
        bucket: Target bucket name (already validated).
        rel: Client-supplied bucket-relative path; ``""`` and ``"."`` refer
            to the bucket root.
        resolve_final: When True (default) the returned path is fully
            resolved — the safe choice for directory targets (uploads) and
            base files (signatures/list).

    Returns:
        The confined absolute path, or ``None`` when the path is unsafe.
    """
    base = f"{get_buckets_path()}/{bucket}"
    if rel in ("", "."):
        return str(Path(base).resolve())
    if resolve_final:
        return resolve_within(base, rel)

    clean = validate_remote_relpath(rel)
    if clean is None:
        return None
    root = Path(base).resolve()
    try:
        parent = (root / Path(clean).parent).resolve()
        target = parent / Path(clean).name
        target.relative_to(root)
    except (OSError, ValueError):
        return None
    return str(target)


async def get_permissions(obj_path: str) -> str:
    """
    Get the file permissions as a canonical octal string.

    Args:
        obj_path: Path to the file or directory.

    Returns:
        Octal permission string (e.g., '644').
    """
    return await to_thread(_read_permissions, obj_path)


async def set_permissions(file_path: str, permissions: str) -> None:
    """
    Set file permissions from a canonical octal string.

    Parses an octal permission string (e.g., '664') and applies the
    corresponding numeric mode to the file. The value comes from a remote
    peer, so special bits (setuid/setgid/sticky) are always stripped: a
    synced file must never become a setuid executable on this host.

    Args:
        file_path: Path to the file.
        permissions: Octal permission string.
    """
    permissions = sanitize_remote_permissions(permissions)
    log.debug(f'Set permissions: "{file_path}" ({permissions})')

    mode = octal_to_mode(permissions)
    await to_thread(chmod, file_path, mode)


async def set_directory_permissions(dir_path: str, permissions: str) -> None:
    """Set directory permissions without making bucket storage inaccessible.

    Directory permissions come from clients as an octal string (for example
    ``755``).  The server must always keep owner rwx access on bucket
    directories so that it can traverse, update and back up its own storage
    tree. Special bits (setuid/setgid/sticky) are stripped first, as the
    value is supplied by a remote peer.
    """
    mode = octal_to_mode(sanitize_remote_permissions(permissions)) | OWNER_DIRECTORY_ACCESS
    log.debug(f'Set directory permissions: "{dir_path}" ({permissions} -> {mode:o})')
    await to_thread(chmod, dir_path, mode)
