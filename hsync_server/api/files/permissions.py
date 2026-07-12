# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
Metadata-only permission endpoint for HSYNC Server.

The full-post upload endpoint and this module share the permission-only
logic through :func:`apply_permissions_to_existing_file`: both apply a
remote ``chmod`` when the content hash already matches and re-broadcast a
causal ``modified`` event, but only this endpoint does it WITHOUT any file
payload crossing the wire. Clients use it for pure metadata convergence
(no bytes are re-read or re-sent).
"""

from pathlib import Path
from typing import Annotated

from fastapi import Depends, Request
from fastapi.responses import JSONResponse
from hsync_common.models.event import Event, EventName, is_valid_cid
from hsync_common.models.file import File as LocalFile
from hsync_common.models.file import is_hidden_file
from hsync_common.path_validation import is_internal_name, is_valid_filename, sanitize_filename
from hsync_common.permissions import is_valid_octal_permissions, sanitize_remote_permissions
from loguru import logger as log

from hsync_server.api.authz import check_bucket_access
from hsync_server.api.connection_manager import bucket_manager
from hsync_server.api.utils import (
    get_permissions,
    is_valid_bucket,
    resolve_within_bucket,
    set_permissions,
)
from hsync_server.auth import AuthContext, verify_api_key
from hsync_server.config import get_buckets_path
from hsync_server.constants import ActionLabel
from hsync_server.exceptions import (
    InvalidBucketNameError,
    InvalidFilenameError,
    InvalidPathError,
    InvalidPermissionFormatError,
    ValidationError,
)
from hsync_server.rate_limiter import RATE_LIMIT_DEFAULT, limiter
from hsync_server.routers import auth_router
from hsync_server.versions import server_version_info


async def apply_permissions_to_existing_file(
    *,
    bucket: str,
    file_location: str,
    cid: str,
    permissions: str,
) -> bool:
    """Apply a permission-only change to an existing file and notify peers.

    Contracts:
    - *permissions* comes from a remote peer, so it is sanitized (special
      setuid/setgid/sticky bits are stripped) and validated. The comparison
      is against the stored file's *canonical* octal mode.
    - When the modes diverge, the new mode is applied and a causal
      ``modified`` event carrying the stored file metadata (STILL with its
      unchanged content hash, joined version store clock) is re-broadcast
      to the other connected clients. Returns ``True``.
    - When identical content already has identical permissions, nothing is
      touched and ``False`` is returned (the caller keeps its legacy 409).
    """
    permissions_only = False
    if permissions and is_valid_octal_permissions(permissions):
        current_permissions = await get_permissions(file_location)
        if current_permissions != sanitize_remote_permissions(permissions):
            await set_permissions(file_location, permissions)
            permissions_only = True
    # A permission-only change must reach the other connected clients:
    # re-emit the (unchanged) file as a `modified` event so they apply
    # just the permissions (their content dedup sees the same hash).
    if permissions_only:
        # Kept synchronous ON PURPOSE: offloading File.get would let a
        # same-path upload interleave and broadcast its generation under this
        # request's CID. The permission-only convergence path is rare, so the
        # pre-existing loop hold is the safer trade-off.
        src = None if is_hidden_file(file_location) else LocalFile.get(file_location)
        if src is not None:
            src.version = server_version_info(bucket, _bucket_dir(bucket), file_location)
        if src:
            manager = bucket_manager.get_manager(bucket)
            await manager.send_message(
                Event(cid, _bucket_dir(bucket), bucket, EventName.modified, src)
            )
    return permissions_only


def _bucket_dir(bucket: str) -> str:
    """Return the absolute storage directory for ``bucket``."""
    return f"{get_buckets_path()}/{bucket}"


@auth_router.patch("/api/v1/buckets/{bucket}/files/permissions")
@limiter.limit(RATE_LIMIT_DEFAULT)
async def patch_file_permissions(
    request: Request,  # Required by rate limiter
    bucket: str,
    ctx: Annotated[AuthContext, Depends(verify_api_key)],
    filename: str,
    permissions: str,
    cid: str = "",
) -> JSONResponse:
    """
    Apply a permission-only change to an existing file (no payload upload).

    Optimized branch of the upload same-SHA path: the client declares the
    filename (bucket-relative), the expected permissions and its CID; the
    server sanitizes/applies the new mode and re-broadcasts a ``modified``
    event to the other connected clients. No file bytes cross the wire.

    Args:
        bucket: Target bucket name.
        filename: Bucket-relative path of the existing file.
        permissions: Octal permission string (e.g., '644'). Special bits
            (setuid/setgid/sticky) are stripped (remote-supplied).
        cid: Client ID for event attribution (validated, optional).

    Returns:
        JSON response: 200 on permissions updated, 404 when the file does
        not exist.

    Raises:
        InvalidBucketNameError: If bucket name is invalid.
        InvalidPathError: If path escapes the bucket.
        InvalidPermissionFormatError: If permission format is invalid.
        InvalidFilenameError: If the basename is forbidden or reserved.
        ValidationError: If the declared CID is malformed.
    """
    check_bucket_access(bucket, ctx)

    if not is_valid_bucket(bucket):
        raise InvalidBucketNameError(details={"bucket": bucket})

    safe_name = sanitize_filename(Path(filename).name or "")
    if not is_valid_filename(safe_name) or is_internal_name(safe_name):
        raise InvalidFilenameError(details={"filename": filename})

    file_location = resolve_within_bucket(bucket, filename)
    if file_location is None:
        raise InvalidPathError(details={"path": filename})

    if cid and not is_valid_cid(cid):
        raise ValidationError(message="Invalid client ID format", details={"cid": cid})

    if permissions and not is_valid_octal_permissions(permissions):
        raise InvalidPermissionFormatError(details={"permissions": permissions})

    if not Path(file_location).exists():
        return JSONResponse(
            status_code=404,
            content={
                "info": "File not found",
                "file": f"{bucket}/{filename}",
            },
        )

    permissions_only = await apply_permissions_to_existing_file(
        bucket=bucket,
        file_location=file_location,
        cid=cid,
        permissions=permissions,
    )
    if permissions_only:
        log.info(
            f'{ActionLabel.PERMISSION} Permissions updated: "{bucket}/{filename}" '
            f"({sanitize_remote_permissions(permissions)})"
        )
        return JSONResponse(
            status_code=200,
            content={
                "info": "Permissions updated",
                "file": f"{bucket}/{filename}",
            },
        )
    return JSONResponse(
        status_code=200,
        content={
            "info": "Permissions already current",
            "file": f"{bucket}/{filename}",
        },
    )
