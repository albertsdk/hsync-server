# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025-2026 Alberto Basso

"""
Symlink creation endpoint for HSYNC Server.

This module provides the REST API endpoint for creating symbolic links
inside a bucket. The peer sends the symlink structurally (path + target)
and the server creates the link, never dereferencing a file or directory.
"""

from os import symlink as os_symlink
from pathlib import Path
from typing import Annotated

from fastapi import Depends, Request
from fastapi.responses import JSONResponse
from hsync_common.path_validation import is_safe_symlink_target
from hsync_common.protocol.rest import REST_SYMLINKS_SUFFIX, rest_bucket_template
from loguru import logger as log

from hsync_server.api.authz import check_bucket_access
from hsync_server.api.utils import resolve_within_bucket
from hsync_server.auth import AuthContext, verify_api_key
from hsync_server.constants import ActionLabel
from hsync_server.exceptions import InvalidPathError
from hsync_server.rate_limiter import RATE_LIMIT_DEFAULT, limiter
from hsync_server.routers import auth_router


@auth_router.post(rest_bucket_template(REST_SYMLINKS_SUFFIX))
@limiter.limit(RATE_LIMIT_DEFAULT)
async def create_symlink(
    request: Request,  # Required by rate limiter
    bucket: str,
    ctx: Annotated[AuthContext, Depends(verify_api_key)],
    path: str,
    target: str,
    cid: str = "",
) -> JSONResponse:
    """Create a symlink inside a bucket.

    The peer sends the symlink structurally (path + target); the server
    creates the link in the filesystem, never dereferencing it. Escaping
    targets are rejected via ``is_safe_symlink_target``.

    Args:
        bucket: Target bucket name.
        path: Bucket-relative path for the symlink to create.
        target: The symlink target (what it points to).
        cid: Optional client ID (for echo suppression on LAN master;
            unused on the central server).

    Returns:
        201 on success, 400 on invalid/unsafe input or non-empty directory
        at the destination.

    Raises:
        InvalidBucketNameError: If bucket name is invalid.
        InvalidPathError: If path is invalid.
    """
    check_bucket_access(bucket, ctx)
    cid_suffix = f" (CID: {cid})" if cid else ""

    if not path:
        raise InvalidPathError(details={"path": path})

    # Confine to *this* bucket: both path and target are untrusted input.
    # The final component is NOT dereferenced: an existing endpoint symlink
    # must be seen as the link (and replaced as such), never as its target.
    absolute = resolve_within_bucket(bucket, path, resolve_final=False)
    if absolute is None:
        raise InvalidPathError(details={"path": path})

    if not target:
        raise InvalidPathError(details={"path": path, "reason": "target is required"})

    if not is_safe_symlink_target(absolute, target, resolve_within_bucket(bucket, "") or ""):
        log.warning(
            f"{ActionLabel.SECURITY} Symlink target escapes bucket, "
            f'not creating: "{absolute}" -> "{target}"'
        )
        raise InvalidPathError(details={"path": path, "reason": "target escapes bucket"})

    # Existing-path cleanup: unlink a file/link, rmdir an empty directory,
    # refuse a non-empty directory.
    dest = Path(absolute)
    if dest.is_dir() and not dest.is_symlink():
        try:
            dest.rmdir()
        except OSError:
            log.warning(
                f"{ActionLabel.SECURITY} Refusing to replace non-empty directory "
                f'with symlink: "{absolute}"{cid_suffix}'
            )
            raise InvalidPathError(
                details={"path": path, "reason": "cannot replace non-empty directory"}
            ) from None
    elif dest.is_symlink() or dest.exists():
        dest.unlink()

    parent = dest.parent
    if not parent.exists():
        parent.mkdir(parents=True, exist_ok=True)

    os_symlink(target, str(dest))
    log.info(f'{ActionLabel.SYMLINK} "{absolute}" -> "{target}"{cid_suffix}')

    return JSONResponse(status_code=201, content={"status": "created"})
