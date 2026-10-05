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
from shutil import disk_usage
from urllib.parse import quote

from fastapi import Request
from hsync_common.path_validation import (
    is_valid_bucket_name,
    resolve_within,
)
from hsync_common.permissions import (
    OWNER_DIRECTORY_ACCESS,
    octal_to_mode,
    sanitize_remote_permissions,
)
from hsync_common.permissions import get_permissions as _read_permissions
from loguru import logger as log

from hsync_server.config import get_buckets_path, get_settings
from hsync_server.exceptions import (
    FileTooLargeError,
    InsufficientStorageError,
    ValidationError,
)


def build_content_disposition(filename: str) -> str:
    """
    Build an RFC 6266 ``Content-Disposition`` value for a download *filename*.

    HTTP header values are encoded as latin-1, so a filename containing any
    character outside that repertoire (e.g. ``č``, ``ł``, CJK) cannot be placed
    verbatim in a quoted ``filename=`` parameter: doing so raises
    ``UnicodeEncodeError`` while the response headers are serialized and turns
    the download into a 500. Non-ASCII names are therefore emitted with the
    ``filename*=utf-8''<pct-encoded>`` form, which is exactly what Starlette's
    :class:`~starlette.responses.FileResponse` does for the identity branch —
    keeping both download branches consistent. Pure-ASCII names keep the plain
    quoted form.

    Args:
        filename: Filename to advertise (a bare basename, not a path).

    Returns:
        The header value, e.g. ``attachment; filename="report.txt"`` or
        ``attachment; filename*=utf-8''dati-%C4%8D``.
    """
    quoted = quote(filename)
    if quoted != filename:
        return f"attachment; filename*=utf-8''{quoted}"
    return f'attachment; filename="{filename}"'


# Bucket-name validation is the hsync-common SSOT (``hsync_common.path_validation``);
# re-exported under the API-layer name so endpoint call sites stay unchanged.
is_valid_bucket = is_valid_bucket_name


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
    return resolve_within(base, rel, resolve_final=resolve_final)


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


def enforce_size_cap(total_bytes: int, max_size: int, *, label: str = "File") -> None:
    """Raise :class:`FileTooLargeError` when *total_bytes* exceeds a cap.

    Shared by the full-upload and delta-apply paths so the anti-DoS size cap
    cannot drift between them. A non-positive cap means "unlimited".
    """
    if max_size > 0 and total_bytes > max_size:
        raise FileTooLargeError(
            message=f"{label} exceeds maximum allowed size of {max_size} bytes",
            details={"max_size": max_size, "size": total_bytes},
        )


def ensure_disk_space(incoming_size: int) -> None:
    """Reject an incoming write that would break the free-space reserve.

    Shared by the full-upload and delta-apply paths so the anti-exhaustion
    guard cannot drift between them. A non-positive reserve disables the check;
    a ``disk_usage`` failure (unreadable path) is a warning, not a rejection.

    Raises:
        InsufficientStorageError: When free space is below reserve + *size*.
    """
    min_free = get_settings().min_free_disk_space
    if min_free <= 0:
        return
    try:
        usage = disk_usage(get_buckets_path())
    except OSError:
        log.warning("Could not check disk space")
        return
    required = min_free + incoming_size
    if usage.free < required:
        free_mb = usage.free / (1024 * 1024)
        required_mb = required / (1024 * 1024)
        raise InsufficientStorageError(
            message=(
                f"Insufficient storage: {free_mb:.0f} MB available, {required_mb:.0f} MB required"
            ),
            details={"available_bytes": usage.free, "required_bytes": required},
        )


async def read_request_body_capped(request: Request, max_bytes: int) -> bytes:
    """Read a request body without ever buffering more than *max_bytes*.

    Streams the body and raises as soon as the running total crosses the cap, so
    an oversized — or chunked, length-less — request is never fully
    materialised in memory first. A ``Content-Length`` over the cap is rejected
    before reading a single byte.

    Raises:
        ValidationError: When the declared or streamed body exceeds the cap.
    """
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            declared_size = int(declared)
        except ValueError:
            declared_size = None
        if declared_size is not None and declared_size > max_bytes:
            raise ValidationError(
                message="Request body too large",
                details={"max_size": max_bytes, "declared_size": declared_size},
            )

    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > max_bytes:
            raise ValidationError(
                message="Request body too large",
                details={"max_size": max_bytes, "received_size": len(body)},
            )
    return bytes(body)
