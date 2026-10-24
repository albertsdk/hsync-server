# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""
File listing and upload status endpoints for HSYNC Server.

This module provides the REST API endpoints for:
- Listing files in a bucket (streamed, path-ordered)
- Listing directory permissions for a bucket (streamed)
- Getting upload status for resumable uploads
"""

import hmac
import json
import os
from collections.abc import Iterator
from os import remove, walk
from pathlib import Path
from typing import Annotated

from fastapi import Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from hsync_common.constants import PARTIAL_DOWNLOAD_SUFFIX
from hsync_common.hashing import is_sha256_hex
from hsync_common.models.dir import Dir, is_hidden_dir
from hsync_common.models.file import File as LocalFile
from hsync_common.models.file import is_hidden_file
from hsync_common.models.symlink import Symlink
from hsync_common.models.version import relpath_in_bucket
from hsync_common.path_validation import is_safe_symlink_target
from hsync_common.protocol.rest import (
    REST_DIRS_SUFFIX,
    REST_FILES_SUFFIX,
    REST_FILES_UPLOAD_STATUS_SUFFIX,
    REST_TOMBSTONES_SUFFIX,
    rest_bucket_template,
)
from loguru import logger as log

from hsync_server.api.authz import check_bucket_access
from hsync_server.api.utils import read_request_body_capped, resolve_within_bucket
from hsync_server.auth import AuthContext, verify_api_key
from hsync_server.config import get_buckets_path
from hsync_server.constants import BACKUP_SUFFIX, ActionLabel
from hsync_server.exceptions import (
    BucketNotFoundError,
    InvalidPathError,
)
from hsync_server.hashes import get_sha256_hash
from hsync_server.rate_limiter import RATE_LIMIT_DEFAULT, limiter
from hsync_server.routers import auth_router
from hsync_server.tombstones import get_tombstone_store
from hsync_server.versions import server_version_info_for_relpath

# Cap on the POST /tombstones body: comfortably above a full
# ``TOMBSTONE_MAX_ENTRIES`` payload, so a hostile member cannot force the
# server to buffer an unbounded body before validation runs.
_MAX_TOMBSTONE_BODY_SIZE = 50 * 1024 * 1024  # 50 MB


@auth_router.get(rest_bucket_template(REST_FILES_UPLOAD_STATUS_SUFFIX))
@limiter.limit(RATE_LIMIT_DEFAULT)
async def get_upload_status(
    request: Request,  # Required by rate limiter
    bucket: str,
    ctx: Annotated[AuthContext, Depends(verify_api_key)],
    filename: str,
    sha256sum: str = "",
    size: int = 0,
) -> JSONResponse:
    """
    Get upload status for resumable uploads.

    Checks if a partial file exists from a previous interrupted upload
    and returns the current offset from which the client can resume.

    Args:
        request: The incoming FastAPI request.
        bucket: Target bucket name.
        filename: Path to the file within the bucket.
        sha256sum: Expected SHA-256 checksum of the complete file.
        size: Expected size of the complete file.

    Returns:
        JSON response with upload status:
        - offset: Number of bytes already uploaded (0 if no partial file)
        - status: "resumable" if partial file exists, "new" otherwise

    Raises:
        InvalidBucketNameError: If bucket name is invalid.
        InvalidPathError: If path is invalid.
    """
    check_bucket_access(bucket, ctx)
    file_path = resolve_within_bucket(bucket, filename)
    if file_path is None:
        raise InvalidPathError(details={"path": filename})

    partial_path = f"{file_path}{PARTIAL_DOWNLOAD_SUFFIX}"

    if (
        sha256sum
        and is_sha256_hex(sha256sum)
        and Path(file_path).exists()
        and hmac.compare_digest(sha256sum, get_sha256_hash(file_path) or "")
    ):
        return JSONResponse(
            status_code=200,
            content={
                "status": "complete",
                "offset": 0,
                "message": "File already exists with same checksum",
            },
        )

    if Path(partial_path).exists():
        partial_size = Path(partial_path).stat().st_size
        if size > 0 and partial_size >= size:
            # Partial file is invalid, remove it
            remove(partial_path)
            return JSONResponse(
                status_code=200,
                content={
                    "status": "new",
                    "offset": 0,
                    "message": "Previous partial file was invalid, starting fresh",
                },
            )
        return JSONResponse(
            status_code=200,
            content={
                "status": "resumable",
                "offset": partial_size,
                "message": f"Can resume from byte {partial_size}",
            },
        )

    return JSONResponse(
        status_code=200,
        content={
            "status": "new",
            "offset": 0,
            "message": "No partial file exists",
        },
    )


def _emit_symlink(file_path: str, bucket_root: str) -> dict | None:
    """Return a bucket-relative symlink entry, or None to skip it."""
    try:
        link = Symlink.get(file_path)
    except OSError:
        return None
    if link is None:
        return None
    if link.target is None or not is_safe_symlink_target(file_path, link.target, bucket_root):
        log.warning(
            f"{ActionLabel.SECURITY} Symlink target escapes bucket, "
            f'skipping: "{file_path}" -> "{link.target}"'
        )
        return None
    rel = os.path.relpath(file_path, bucket_root)
    entry = link.to_dict()
    entry["filepath"] = rel
    return entry


def _iter_json_array(items: Iterator[dict]) -> Iterator[bytes]:
    """Yield a JSON array body incrementally: ``[``, item, ``,``, item, ``]``.

    ``StreamingResponse`` iterates this sync generator on a worker thread, so the
    body is written as it is produced and never assembled into one ``str``/
    ``bytes`` (the previous ``JSONResponse`` held the whole array *and* its
    serialised copy). A producer failure truncates the array — invalid JSON — so a
    consumer fails loudly instead of acting on a silently partial listing.
    """
    yield b"["
    first = True
    for item in items:
        if first:
            first = False
        else:
            yield b","
        yield json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    yield b"]"


def _iter_dir_entries(bucket_root: str) -> Iterator[dict]:
    """Yield one ``{path, permissions}`` entry per directory, in path order."""
    for root, dirs, _files in walk(bucket_root):
        # Skip hidden directories and keep the walk deterministic (sorted) so the
        # stream order is stable without buffering the tree.
        dirs[:] = sorted(d for d in dirs if not is_hidden_dir(f"{root}/{d}"))
        for name in dirs:
            full = f"{root}/{name}"
            # Dir-symlinks are structural entries, never listed as directories.
            if os.path.islink(full):
                continue
            dir_obj = Dir.get(full)
            if dir_obj is None or dir_obj.permissions is None:
                continue
            rel = os.path.relpath(full, bucket_root)
            if rel == ".":
                continue
            yield {"path": rel, "permissions": dir_obj.permissions}


def _iter_file_entries(root_dir: str, bucket_root: str, bucket: str) -> Iterator[dict]:
    """Yield one file metadata dict per listing entry, in path order.

    Runs as a sync generator, so ``StreamingResponse`` iterates it on a worker
    thread: ``LocalFile.get`` (stat + SHA-256 on a cold cache) never blocks the
    event loop, and the listing is never materialised — peak memory is
    O(directory fanout + one entry) instead of one dict per file.

    Order is the deterministic path order (sorted entries); the ``mtime``
    priority is applied by the consumer after it has collected the entries.
    """
    for root, dirs, files in walk(root_dir):
        dirs[:] = sorted(d for d in dirs if not is_hidden_dir(f"{root}/{d}"))

        for file in sorted(files):
            file_path = f"{root}/{file}"
            if is_hidden_file(file_path):
                continue
            # Backup files (kept during upload to preserve read access).
            if file.endswith(BACKUP_SUFFIX):
                continue

            # Symlinks are emitted as symlink entries (never dereferenced): a peer
            # recreates the link structurally, and an escaping symlink is skipped.
            if os.path.islink(file_path):
                entry = _emit_symlink(file_path, bucket_root)
                if entry is not None:
                    yield entry
                continue

            file_obj = LocalFile.get(file, root)
            if file_obj:
                file_data = file_obj.to_dict()
                abs_path = file_obj.filepath or ""
                rel = relpath_in_bucket(abs_path, bucket_root)
                file_data["filepath"] = abs_path if rel is None else rel
                # JOIN causal version metadata from the version store (ADR-0022).
                version = server_version_info_for_relpath(bucket, rel) if rel is not None else None
                if version is not None:
                    file_data["version"] = version
                yield file_data

        # Dir-symlink entries: os.walk (followlinks=False) lists them in ``dirs``
        # but never descends into them.
        for d in dirs:
            d_path = f"{root}/{d}"
            if os.path.islink(d_path):
                entry = _emit_symlink(d_path, bucket_root)
                if entry is not None:
                    yield entry


@auth_router.get(rest_bucket_template(REST_DIRS_SUFFIX))
@limiter.limit(RATE_LIMIT_DEFAULT)
async def get_dirs(
    request: Request,  # Required by rate limiter
    bucket: str,
    ctx: Annotated[AuthContext, Depends(verify_api_key)],
) -> StreamingResponse:
    """
    List directory metadata (path + permissions) in a bucket.

    Directories carry no content hash and never appear in the ``/files``
    listing, so this dedicated endpoint exposes their permissions for the
    download-only permission converge ("remote wins"): a peer applies a
    divergent directory mode locally without any per-file event having
    arrived.

    Args:
        bucket: Name of the bucket to list.

    Returns:
        Streamed JSON array (chunked) of ``{"path": <bucket-relative>,
        "permissions": <octal>}`` entries, in deterministic path order.

    Raises:
        InvalidBucketNameError: If bucket name is invalid.
        BucketNotFoundError: If bucket does not exist.
    """
    check_bucket_access(bucket, ctx)

    buckets_path = get_buckets_path()
    bucket_root = str(Path(f"{buckets_path}/{bucket}").resolve())
    if not Path(bucket_root).exists():
        raise BucketNotFoundError(details={"bucket": bucket, "path": ""})

    return StreamingResponse(
        _iter_json_array(_iter_dir_entries(bucket_root)),
        media_type="application/json",
    )


@auth_router.get(rest_bucket_template(REST_FILES_SUFFIX))
@limiter.limit(RATE_LIMIT_DEFAULT)
async def get_files(
    request: Request,  # Required by rate limiter
    bucket: str,
    ctx: Annotated[AuthContext, Depends(verify_api_key)],
    path: str | None = None,
) -> StreamingResponse:
    """
    List all files in a bucket or subdirectory.

    Recursively walks the directory tree and streams metadata for all
    non-hidden files as a JSON array, in deterministic path order. The array is
    produced incrementally (chunked), so the server holds O(directory fanout +
    one entry) rather than one dict per file; consumers collect the whole array
    and apply the ``mtime`` priority locally.

    Args:
        bucket: Name of the bucket to list files from.
        path: Optional subdirectory path within the bucket.

    Returns:
        Streamed JSON array of file metadata dictionaries.

    Raises:
        InvalidBucketNameError: If bucket name is invalid.
        InvalidPathError: If path is invalid.
        BucketNotFoundError: If bucket does not exist.
    """
    check_bucket_access(bucket, ctx)
    # Confine to *this* bucket: the filter path is untrusted input and must
    # never escape into a sibling bucket or outside the storage root.
    dir = resolve_within_bucket(bucket, path or "")
    if dir is None:
        raise InvalidPathError(details={"path": path})

    buckets_path = get_buckets_path()
    # Absolute, resolved root: every path compared against it (walk root and
    # file paths inside) is built the same resolved way, so the wire listing
    # always carries bucket-relative paths. A raw (possibly relative) config
    # string here would make File.from_dict's relative_to fall back to the
    # verbatim ABSOLUTE path, which peers then reject as unsafe.
    bucket_root = str(Path(f"{buckets_path}/{bucket}").resolve())

    # Validate existence *before* the first byte: once the stream starts the
    # status is already sent, so a missing bucket must fail as a normal HTTP
    # error here.
    if not Path(dir).exists():
        raise BucketNotFoundError(details={"bucket": bucket, "path": path})

    return StreamingResponse(
        _iter_json_array(_iter_file_entries(dir, bucket_root, bucket)),
        media_type="application/json",
    )


@auth_router.get(rest_bucket_template(REST_TOMBSTONES_SUFFIX))
@limiter.limit(RATE_LIMIT_DEFAULT)
async def get_tombstones(
    request: Request,  # Required by rate limiter
    bucket: str,
    ctx: Annotated[AuthContext, Depends(verify_api_key)],
) -> JSONResponse:
    """
    List deletion markers (tombstones) for a bucket.

    Returns a mapping of hashed bucket-relative path to the causal marker
    (``{"sha256", "version_id", "origin_cid", "ts", "vclock"}``). Clients use
    these markers to avoid resurrecting files that were deleted while they
    were offline.

    Args:
        request: The incoming FastAPI request.
        bucket: Name of the bucket to list tombstones from.

    Returns:
        JSON response with the tombstone payload.

    Raises:
        InvalidBucketNameError: If bucket name is invalid.
        BucketNotFoundError: If bucket does not exist.
    """
    check_bucket_access(bucket, ctx)
    store = get_tombstone_store(bucket)
    store.prune()

    return JSONResponse(
        status_code=200,
        content=store.to_payload(),
    )


@auth_router.post(rest_bucket_template(REST_TOMBSTONES_SUFFIX))
@limiter.limit(RATE_LIMIT_DEFAULT)
async def post_tombstones(
    request: Request,  # Required by rate limiter
    bucket: str,
    ctx: Annotated[AuthContext, Depends(verify_api_key)],
) -> JSONResponse:
    """
    Merge deletion markers (tombstones) pushed by a client.

    A client that deleted files while offline pushes its accumulated markers
    at the start of each sync cycle so the server converges to the union of
    all known deletions and other peers do not resurrect those files. The
    merge is **causal** per marker (vector-clock dominance, deterministic
    content tie-break on concurrency — never the deletion timestamp, ADR-0003);
    entries are strictly validated and the store size is capped (see
    ``TombstoneStore.merge``).

    Args:
        request: The incoming FastAPI request; the body is the JSON tombstone
            payload (mapping of hashed path to the causal
            ``{"sha256", "version_id", "origin_cid", "ts", "vclock"}`` entry).
        bucket: Name of the bucket to merge markers into.

    Returns:
        JSON response with the number of markers accepted/changed.

    Raises:
        InvalidBucketNameError: If bucket name is invalid.
        BucketNotFoundError: If bucket does not exist.
        InvalidPathError: If the body is not a JSON object.
    """
    check_bucket_access(bucket, ctx)
    body_bytes = await read_request_body_capped(request, _MAX_TOMBSTONE_BODY_SIZE)
    try:
        payload = json.loads(body_bytes)
    except (ValueError, UnicodeDecodeError):
        raise InvalidPathError(details={"reason": "invalid JSON body"})
    if not isinstance(payload, dict):
        raise InvalidPathError(details={"reason": "tombstone payload must be a JSON object"})

    store = get_tombstone_store(bucket)
    changed = store.merge(payload)
    if changed:
        store.prune()
        store.save()

    return JSONResponse(status_code=200, content={"merged": changed})
