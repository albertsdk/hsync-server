# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""
Metadata-only permission endpoint for HSYNC Server.

The full-post upload endpoint and this module share the metadata-only
logic through :func:`apply_metadata_to_existing_file`: both apply a remote
``chmod`` and/or ``touch`` when the content hash already matches and
re-broadcast a causal ``modified`` event, but only this endpoint does it
WITHOUT any file payload crossing the wire. Clients use it for pure metadata
convergence (no bytes are re-read or re-sent).
"""

import hmac
import json
from pathlib import Path
from typing import Annotated

from fastapi import Depends, Request
from fastapi.responses import JSONResponse
from hsync_common.constants import MTIME_PROPAGATION_TOLERANCE_SECONDS
from hsync_common.hashing import compute_sha256_async, is_sha256_hex
from hsync_common.models.event import Event, EventName, is_valid_cid
from hsync_common.models.file import File as LocalFile
from hsync_common.models.file import is_hidden_file
from hsync_common.path_validation import is_internal_name, is_valid_filename, sanitize_filename
from hsync_common.permissions import is_valid_octal_permissions, sanitize_remote_permissions
from hsync_common.protocol.rest import REST_FILES_PERMISSIONS_SUFFIX, rest_bucket_template
from loguru import logger as log

from hsync_server.api.authz import check_bucket_access
from hsync_server.api.connection_manager import bucket_manager
from hsync_server.api.file_utils import update_file_timestamp
from hsync_server.api.utils import (
    get_permissions,
    resolve_within_bucket,
    set_permissions,
)
from hsync_server.auth import AuthContext, verify_api_key
from hsync_server.config import get_buckets_path
from hsync_server.constants import ActionLabel
from hsync_server.exceptions import (
    ConflictError,
    InvalidFilenameError,
    InvalidPathError,
    InvalidPermissionFormatError,
    ValidationError,
)
from hsync_server.hashes import get_sha256_hash, set_sha256_hash
from hsync_server.rate_limiter import RATE_LIMIT_DEFAULT, limiter
from hsync_server.routers import auth_router
from hsync_server.versions import record_version, server_version_info


async def apply_metadata_to_existing_file(
    *,
    bucket: str,
    file_location: str,
    cid: str,
    permissions: str,
    mtime: int = 0,
    vclock: dict[str, int] | None = None,
    version_id: str | None = None,
) -> bool:
    """Apply a content-unchanged metadata change (mode and/or mtime) to a file.

    Contracts:
    - *permissions* comes from a remote peer, so it is sanitized (special
      setuid/setgid/sticky bits are stripped) and validated. The comparison
      is against the stored file's *canonical* octal mode.
    - *mtime* is a peer-supplied corrected Unix timestamp, applied only when it
      diverges from the stored mtime by more than
      :data:`MTIME_PROPAGATION_TOLERANCE_SECONDS` (a plain ``touch``). The
      far-future clamp inside :func:`update_file_timestamp` guards abuse.
    - When either diverges, the change is applied and a causal ``modified``
      event carrying the stored file metadata (STILL with its unchanged content
      hash, joined version store clock) is re-broadcast to the other connected
      clients. Returns ``True``.
    - When identical content already has identical metadata, nothing is touched
      and ``False`` is returned (the caller keeps its legacy 409).
    """
    changed = False
    if permissions and is_valid_octal_permissions(permissions):
        current_permissions = await get_permissions(file_location)
        if current_permissions != sanitize_remote_permissions(permissions):
            await set_permissions(file_location, permissions)
            changed = True
    if mtime > 0:
        try:
            current_mtime = int(Path(file_location).stat().st_mtime)
        except OSError:
            current_mtime = 0
        if abs(current_mtime - mtime) > MTIME_PROPAGATION_TOLERANCE_SECONDS:
            # The utime pass changes the mtime_ns the hash cache keys on:
            # preserve the (unchanged) digest so the next same-SHA upload still
            # hits the dup-skip instead of re-hashing.
            cached_hash = get_sha256_hash(file_location)
            update_file_timestamp(file_location, mtime=str(mtime))
            if cached_hash:
                set_sha256_hash(file_location, cached_hash)
            changed = True
    # A metadata-only change must reach the other connected clients:
    # re-emit the (unchanged) file as a `modified` event so they apply
    # just the metadata (their content dedup sees the same hash).
    if changed:
        # Record the metadata change as a real causal version. Without this the
        # version store keeps the old corrected mtime and clock, so every later
        # sync re-detects the divergence and re-sends the metadata (churn) and
        # the listing advertises stale metadata. The content hash is unchanged.
        record_version(
            bucket,
            _bucket_dir(bucket),
            file_location,
            base_hash=get_sha256_hash(file_location) or "",
            vclock=vclock,
            version_id=version_id,
            origin_cid=cid or None,
            corrected_mtime=mtime,
        )
        # Kept synchronous ON PURPOSE: offloading File.get would let a
        # same-path upload interleave and broadcast its generation under this
        # request's CID. The metadata-only convergence path is rare, so the
        # pre-existing loop hold is the safer trade-off.
        src = None if is_hidden_file(file_location) else LocalFile.get(file_location)
        if src is not None:
            src.version = server_version_info(bucket, _bucket_dir(bucket), file_location)
        if src:
            manager = bucket_manager.get_manager(bucket)
            await manager.send_message(
                Event(cid, _bucket_dir(bucket), bucket, EventName.modified, src)
            )
    return changed


def _bucket_dir(bucket: str) -> str:
    """Return the absolute storage directory for ``bucket``."""
    return f"{get_buckets_path()}/{bucket}"


@auth_router.patch(rest_bucket_template(REST_FILES_PERMISSIONS_SUFFIX))
@limiter.limit(RATE_LIMIT_DEFAULT)
async def patch_file_permissions(
    request: Request,  # Required by rate limiter
    bucket: str,
    ctx: Annotated[AuthContext, Depends(verify_api_key)],
    filename: str,
    permissions: str,
    cid: str = "",
    mtime: int = 0,
    vclock: str = "",
    version_id: str = "",
    expected_sha256: str = "",
) -> JSONResponse:
    """
    Apply a content-unchanged metadata change to an existing file (no payload).

    Optimized branch of the upload same-SHA path: the client declares the
    filename (bucket-relative), the expected permissions, an optional corrected
    ``mtime``, the content hash it believes the path still holds and its CID;
    the server sanitizes/applies what diverges and re-broadcasts a ``modified``
    event to the other connected clients. No file bytes cross the wire. Both a
    chmod and a plain ``touch`` (same content, newer mtime) converge through
    this route.

    Args:
        bucket: Target bucket name.
        filename: Bucket-relative path of the existing file.
        permissions: Octal permission string (e.g., '644'). Special bits
            (setuid/setgid/sticky) are stripped (remote-supplied).
        cid: Client ID for event attribution (validated, optional).
        mtime: Corrected Unix mtime to propagate (0 = leave unchanged).
        vclock: JSON-encoded effective vector clock of the sender ("" = none).
        version_id: Effective version id of the sender ("" = none).
        expected_sha256: Optional guard. The caller only reached this branch
            because its content matched the listing; when the path no longer
            holds that content a concurrent write happened, so the metadata is
            NOT applied and the caller falls back to the ordinary upload path.

    Returns:
        JSON response: 200 on metadata updated, 404 when the file does
        not exist.

    Raises:
        InvalidBucketNameError: If bucket name is invalid.
        InvalidPathError: If path escapes the bucket.
        InvalidPermissionFormatError: If permission format is invalid.
        InvalidFilenameError: If the basename is forbidden or reserved.
        ValidationError: If the declared CID or checksum is malformed.
        ConflictError: If the path's content no longer matches the declaration.
    """
    check_bucket_access(bucket, ctx)

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

    parsed_vclock: dict[str, int] | None = None
    if vclock:
        try:
            raw_vclock = json.loads(vclock)
        except (ValueError, TypeError):
            raise ValidationError(message="Invalid vclock JSON", details={"vclock": vclock})
        if not isinstance(raw_vclock, dict):
            raise ValidationError(message="Invalid vclock JSON", details={"vclock": vclock})
        parsed_vclock = {
            str(k): int(v)
            for k, v in raw_vclock.items()
            if isinstance(v, int) and not isinstance(v, bool)
        }

    if not Path(file_location).exists():
        return JSONResponse(
            status_code=404,
            content={
                "info": "File not found",
                "file": f"{bucket}/{filename}",
            },
        )

    if expected_sha256:
        # The caller reached this branch because its content matched the
        # listing; if the path no longer holds that content (a concurrent
        # upload), applying the mode/mtime would touch bytes nobody saw. The
        # 409 makes the caller fall back to the ordinary upload path, which
        # resolves the content first.
        if not is_sha256_hex(expected_sha256):
            raise ValidationError(
                message="Invalid expected_sha256",
                details={"filename": filename},
            )
        current_sha = get_sha256_hash(file_location)
        if not current_sha:
            # Cold cache: hash off the loop rather than on it.
            current_sha = await compute_sha256_async(file_location)
        if not current_sha or not hmac.compare_digest(current_sha, expected_sha256.lower()):
            raise ConflictError(
                message="File content changed; metadata not applied",
                details={"bucket": bucket, "filename": filename},
            )

    changed = await apply_metadata_to_existing_file(
        bucket=bucket,
        file_location=file_location,
        cid=cid,
        permissions=permissions,
        mtime=max(0, int(mtime)),
        vclock=parsed_vclock,
        version_id=version_id or None,
    )
    if changed:
        log.info(
            f'{ActionLabel.PERMISSION} Metadata updated: "{bucket}/{filename}" '
            f"({sanitize_remote_permissions(permissions)}, mtime={max(0, int(mtime))})"
        )
        return JSONResponse(
            status_code=200,
            content={
                "info": "Metadata updated",
                "file": f"{bucket}/{filename}",
            },
        )
    return JSONResponse(
        status_code=200,
        content={
            "info": "Metadata already current",
            "file": f"{bucket}/{filename}",
        },
    )
