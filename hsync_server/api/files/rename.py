# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""
Rename/move endpoint for HSYNC Server.

A rename performed while a peer was offline reaches that peer's reconcile loop
as "delete the old path" plus "create the new one". The peer already holds both
content hashes — the listing it fetched advertises them — so it calls this
endpoint instead of DELETE + full re-upload, and identical bytes never cross the
wire. The P2P master exposes the same contract
(``hsync_client.p2p_server.P2PServerHandler._handle_rename``), so a single wire
shape serves both transports.

Causal bookkeeping mirrors the live ``renamed`` WebSocket handler
(``hsync_server.api.events_handlers.handlers.handle_renamed_event``): a rename
vacates the source exactly like a deletion (tombstone) and repopulates the
destination exactly like a creation (the stored version is carried over without
a tick, stale deletion markers are dropped). A rename is not a content edit.
"""

import hmac
from copy import deepcopy
from os import replace
from pathlib import Path
from typing import Annotated

from fastapi import Depends, Request
from fastapi.responses import JSONResponse
from hsync_common.hashing import is_sha256_hex
from hsync_common.listing import file_entry
from hsync_common.models.dir import Dir
from hsync_common.models.event import Event, EventName, is_valid_cid
from hsync_common.models.file import File, invalidate_sha256_cache
from hsync_common.models.version import relpath_in_bucket
from hsync_common.permissions import is_valid_octal_permissions
from hsync_common.protocol.rest import REST_FILES_SUFFIX, rest_bucket_template
from loguru import logger as log

from hsync_server.api.authz import check_bucket_access
from hsync_server.api.connection_manager import bucket_manager
from hsync_server.api.file_utils import ensure_sentinel_in_dir, update_file_timestamp
from hsync_server.api.utils import (
    resolve_within_bucket,
    set_directory_permissions,
    set_permissions,
)
from hsync_server.auth import AuthContext, verify_api_key
from hsync_server.config import get_buckets_path
from hsync_server.constants import ActionLabel
from hsync_server.exceptions import (
    ConflictError,
    FileOperationError,
    InvalidPathError,
    InvalidPermissionFormatError,
    ValidationError,
)
from hsync_server.exceptions import FileNotFoundError as HSyncFileNotFoundError
from hsync_server.hashes import set_sha256_hash
from hsync_server.rate_limiter import RATE_LIMIT_DEFAULT, limiter
from hsync_server.routers import auth_router
from hsync_server.tombstones import (
    clear_deletion,
    clear_deletion_tree,
    record_deletion_tree,
    record_path_deletion,
)
from hsync_server.versions import move_version, move_version_prefix, server_version_info


@auth_router.patch(rest_bucket_template(REST_FILES_SUFFIX))
@limiter.limit(RATE_LIMIT_DEFAULT)
async def rename_file(
    request: Request,  # Required by rate limiter
    bucket: str,
    ctx: Annotated[AuthContext, Depends(verify_api_key)],
    src: str,
    dst: str,
    cid: str = "",
    expected_sha256: str = "",
    mtime: int = 0,
    permissions: str = "",
) -> JSONResponse:
    """Move a file or directory inside a bucket without transferring its bytes.

    The caller (a peer running its reconcile loop) has already established that
    the destination holds the same content as the stale source path; this
    endpoint performs the move atomically and records the causal consequences,
    so offline peers observe a rename instead of a delete plus an upload.

    Args:
        bucket: Target bucket name.
        src: Bucket-relative path of the existing entry.
        dst: Bucket-relative path the entry must move to. It must not exist:
            a concurrent creator is reported as a conflict, never overwritten.
        cid: Optional originator client ID (echo suppression for the
            ``renamed`` broadcast).
        expected_sha256: Optional guard. When given for a regular file, the
            move is refused unless the current content still hashes to this
            value — a stale listing must fall back to a normal upload instead
            of moving bytes the peer did not verify.
        mtime: Optional modification time (Unix epoch seconds) applied to the
            destination, so a rename converges the local metadata an upload
            would have carried.
        permissions: Optional octal permissions applied to the destination,
            with the same rationale.

    Returns:
        The destination entry (``file``, listing shape: ``filepath`` for a file,
        ``path``/``permissions`` for a directory) plus the causal ``version`` the
        caller must record as the new merge base.

    Raises:
        InvalidBucketNameError: If bucket name is invalid.
        InvalidPathError: If either path is unsafe, or a symlink rename is
            attempted (links replicate through the symlink surface).
        HSyncFileNotFoundError: If the source does not exist.
        ConflictError: If the destination exists or the checksum guard fails.
        FileOperationError: If the filesystem move fails.
    """
    check_bucket_access(bucket, ctx)

    if not src or not dst:
        raise InvalidPathError(details={"path": src or dst})
    if src.strip("/") == dst.strip("/"):
        raise InvalidPathError(details={"path": src, "reason": "source equals destination"})
    if expected_sha256 and not is_sha256_hex(expected_sha256):
        raise ValidationError(message="Invalid expected_sha256", details={"filename": src})
    if mtime < 0:
        raise ValidationError(message="Invalid mtime", details={"filename": src})
    if permissions and not is_valid_octal_permissions(permissions):
        raise InvalidPermissionFormatError(details={"permissions": permissions})

    # Both endpoints are peer-supplied and untrusted: confine them to *this*
    # bucket. resolve_final=False keeps a link at either end as the LINK entry,
    # so a move never dereferences it into the target file.
    src_path = resolve_within_bucket(bucket, src, resolve_final=False)
    dst_path = resolve_within_bucket(bucket, dst, resolve_final=False)
    if src_path is None:
        raise InvalidPathError(details={"path": src})
    if dst_path is None:
        raise InvalidPathError(details={"path": dst})

    src_obj = Path(src_path)
    # A dangling link is a real entry: ``exists()`` follows the link and would
    # report a genuinely present rename source as missing.
    if not (src_obj.is_symlink() or src_obj.exists()):
        raise HSyncFileNotFoundError(details={"bucket": bucket, "filename": src})
    if src_obj.is_symlink():
        # Links replicate structurally (path + target), never as content: a
        # "rename" would have to be a symlink create + delete pair, which the
        # symlink surface already expresses without moving any bytes.
        raise InvalidPathError(
            details={"path": src, "reason": "symlinks replicate structurally, not by rename"}
        )

    is_dir = src_obj.is_dir()
    local_bucket = f"{get_buckets_path()}/{bucket}"
    origin_cid = cid if is_valid_cid(cid) else ""

    # Everything below runs without an ``await`` between the destination check
    # and the move. On one event loop that makes "destination absent" +
    # "rename" atomic with respect to every other request, so a concurrently
    # created file is never silently overwritten — the same reasoning that
    # keeps the delete path synchronous.
    src_meta: File | None = None
    try:
        if Path(dst_path).is_symlink() or Path(dst_path).exists():
            raise ConflictError(
                message="Destination already exists",
                details={"bucket": bucket, "filename": dst},
            )

        if not is_dir:
            src_meta = File.get(src_path)
            if src_meta is None:
                # Vanished between the existence check and here.
                raise HSyncFileNotFoundError(details={"bucket": bucket, "filename": src})
            if expected_sha256 and (
                not src_meta.sha256sum
                or not hmac.compare_digest(expected_sha256, src_meta.sha256sum)
            ):
                raise ConflictError(
                    message="Source content no longer matches the expected checksum",
                    details={"bucket": bucket, "filename": src},
                )

        # Tombstones first, exactly like the delete endpoint: a crash between
        # the marker and the move leaves a harmless marker (the next upload
        # clears it), while the reverse order could lose the deletion proof and
        # let an offline peer resurrect the vacated path.
        if is_dir:
            # The version entries are carried to the destination below, so they
            # must survive this tombstone pass.
            record_deletion_tree(bucket, local_bucket, src_path, cid=cid, clear_versions=False)
        else:
            record_path_deletion(
                bucket,
                local_bucket,
                src_path,
                sha256sum=src_meta.sha256sum if src_meta else None,
                cid=cid,
            )

        dst_parent = Path(dst_path).parent
        if not dst_parent.exists():
            dst_parent.mkdir(parents=True, exist_ok=True)

        replace(src_path, dst_path)
        invalidate_sha256_cache(src_path)
        invalidate_sha256_cache(dst_path)

        # Causal ownership moves with the content: a rename is not an edit, so
        # the stored clock is carried over unchanged (no tick). The destination
        # may carry markers from an earlier delete; the rename repopulates it,
        # so they must be dropped or peers would treat it as deleted.
        if is_dir:
            move_version_prefix(bucket, local_bucket, src_path, dst_path)
            clear_deletion_tree(bucket, local_bucket, dst_path)
        else:
            move_version(bucket, local_bucket, src_path, dst_path)
            clear_deletion(bucket, local_bucket, dst_path)
    except OSError as e:
        # Only the exception type is exposed: the raw message embeds absolute
        # filesystem paths.
        log.error(f'Rename failed for "{src}" -> "{dst}": {e}')
        raise FileOperationError(
            message="Rename failed",
            details={"src": src, "dst": dst, "error": type(e).__name__},
        ) from e

    # The move vacated the source path: keep the (possibly now empty) parent
    # directory alive with a sentinel, exactly like a delete does.
    ensure_sentinel_in_dir(str(Path(src_path).parent))

    # Metadata converges what an upload would have carried: the moved object
    # still holds the source's timestamps and mode until applied here.
    dst_model: File | Dir | None
    if not is_dir:
        if mtime > 0:
            update_file_timestamp(dst_path, str(int(mtime)))
        if permissions:
            await set_permissions(dst_path, permissions)
        if src_meta is not None and src_meta.sha256sum:
            # The content did not change: prime the digest for the new path
            # instead of re-hashing the moved file on the next listing.
            set_sha256_hash(dst_path, src_meta.sha256sum)
        dst_model = File.get(dst_path)
    else:
        if permissions:
            await set_directory_permissions(dst_path, permissions)
        dst_model = Dir.get(dst_path)

    content: dict = {"info": "Renamed successfully"}
    if dst_model is not None:
        if is_dir:
            content["file"] = {
                "path": relpath_in_bucket(dst_model.path or "", local_bucket) or "",
                "permissions": dst_model.permissions,
            }
        else:
            # Built before the broadcast: constructing the Event strips the
            # bucket prefix from the model in place.
            content["file"] = file_entry(dst_model, local_bucket)
            version = server_version_info(bucket, local_bucket, dst_path)
            if version is not None:
                content["version"] = version

    log.info(f'{ActionLabel.RENAME} "{src}" -> "{dst}" (CID: {origin_cid})')

    if dst_model is not None:
        # The source model reuses the destination's metadata with the vacated
        # path, exactly like the live rename handler: the content is identical
        # by definition of a move.
        src_model = deepcopy(dst_model)
        if is_dir:
            src_model.path = src_path
        else:
            src_model.filepath = src_path
        manager = bucket_manager.get_manager(bucket)
        await manager.send_message(
            Event(origin_cid, local_bucket, bucket, EventName.renamed, src_model, dst_model)
        )

    return JSONResponse(status_code=200, content=content)
