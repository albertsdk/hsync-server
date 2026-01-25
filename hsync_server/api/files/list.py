# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
File listing and upload status endpoints for HSYNC Server.

This module provides the REST API endpoints for:
- Listing files in a bucket with sorting
- Getting upload status for resumable uploads
"""

import hmac
import os
from os import remove, walk
from pathlib import Path

from fastapi import Request
from fastapi.responses import JSONResponse
from hsync_common.constants import PARTIAL_DOWNLOAD_SUFFIX
from hsync_common.hashing import is_sha256_hex
from hsync_common.models.dir import is_hidden_dir
from hsync_common.models.file import File as LocalFile
from hsync_common.models.file import is_hidden_file
from hsync_common.models.symlink import Symlink
from hsync_common.path_validation import is_safe_symlink_target
from loguru import logger as log

from hsync_server.api.utils import is_valid_bucket, resolve_within_bucket
from hsync_server.config import get_buckets_path
from hsync_server.constants import ActionLabel
from hsync_server.exceptions import (
    BucketNotFoundError,
    InvalidBucketNameError,
    InvalidPathError,
)
from hsync_server.hashes import get_sha256_hash
from hsync_server.rate_limiter import RATE_LIMIT_DEFAULT, limiter
from hsync_server.routers import auth_router
from hsync_server.tombstones import get_tombstone_store
from hsync_server.versions import server_version_info


@auth_router.get("/api/v1/buckets/{bucket}/files/upload-status")
@limiter.limit(RATE_LIMIT_DEFAULT)
async def get_upload_status(
    request: Request,  # Required by rate limiter
    bucket: str,
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
    if not is_valid_bucket(bucket):
        raise InvalidBucketNameError(details={"bucket": bucket})
    file_path = resolve_within_bucket(bucket, filename)
    if file_path is None:
        raise InvalidPathError(details={"path": filename})

    partial_path = f"{file_path}{PARTIAL_DOWNLOAD_SUFFIX}"

    # Check if complete file already exists with same SHA-256
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

    # Check for partial file
    if Path(partial_path).exists():
        partial_size = Path(partial_path).stat().st_size
        # Validate partial file is not larger than expected
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


@auth_router.get("/api/v1/buckets/{bucket}/files")
@limiter.limit(RATE_LIMIT_DEFAULT)
async def get_files(
    request: Request,  # Required by rate limiter
    bucket: str,
    path: str | None = None,
) -> JSONResponse:
    """
    List all files in a bucket or subdirectory.

    Recursively walks the directory tree and returns metadata for
    all non-hidden files, sorted by mtime ascending (oldest first)
    to prioritize updating older files.

    Args:
        bucket: Name of the bucket to list files from.
        path: Optional subdirectory path within the bucket.

    Returns:
        JSON response with list of file metadata dictionaries.

    Raises:
        InvalidBucketNameError: If bucket name is invalid.
        InvalidPathError: If path is invalid.
        BucketNotFoundError: If bucket does not exist.
    """
    # Validate inputs
    if not is_valid_bucket(bucket):
        raise InvalidBucketNameError(details={"bucket": bucket})

    # Confine to *this* bucket: the filter path is untrusted input and must
    # never escape into a sibling bucket or outside the storage root.
    dir = resolve_within_bucket(bucket, path or "")
    if dir is None:
        raise InvalidPathError(details={"path": path})

    buckets_path = get_buckets_path()
    bucket_root = f"{buckets_path}/{bucket}"

    if not Path(dir).exists():
        raise BucketNotFoundError(details={"bucket": bucket, "path": path})

    # Collect file metadata
    file_list = []
    for root, dirs, files in walk(dir):
        # Skip hidden directories using is_hidden_dir
        dirs[:] = [d for d in dirs if not is_hidden_dir(f"{root}/{d}")]

        for file in files:
            file_path = f"{root}/{file}"
            # Skip hidden files using is_hidden_file
            if is_hidden_file(file_path):
                continue
            # Skip backup files (used during upload to maintain read access)
            if file.endswith(".backup"):
                continue

            # Symlinks are emitted as symlink entries (never dereferenced).
            # A peer will recreate the link structurally from the target field,
            # and a escaping symlink is skipped entirely.
            if Path(file_path).is_symlink():
                try:
                    link = Symlink.get(file_path)
                except OSError:
                    link = None
                if link is not None and not is_safe_symlink_target(
                    file_path, link.target, bucket_root
                ):
                    log.warning(
                        f"{ActionLabel.SECURITY} Symlink target escapes bucket, "
                        f'skipping: "{file_path}" -> "{link.target}"'
                    )
                    continue
                if link is not None:
                    # Normalize to bucket-relative filepath (mirror File.from_dict).
                    rel = os.path.relpath(file_path, bucket_root)
                    entry = link.to_dict()
                    entry["filepath"] = rel
                    file_list.append(entry)
                continue

            # Get file info and normalize path relative to bucket
            file_obj = LocalFile.get(file, root)
            if file_obj:
                file_data = LocalFile.from_dict(file_obj.to_dict(), bucket_root).to_dict()
                # JOIN causal version metadata from the version store (ADR-0022).
                abs_path = f"{bucket_root}/{file_data.get('filepath', '')}".replace("//", "/")
                version = server_version_info(bucket, bucket_root, abs_path)
                if version is not None:
                    file_data["version"] = version
                file_list.append(file_data)

        # Emit dir-symlink entries: os.walk (followlinks=False) lists them in
        # ``dirs`` but never descends into them. Emit a symlink entry for each.
        for d in dirs:
            d_path = f"{root}/{d}"
            if Path(d_path).is_symlink():
                try:
                    link = Symlink.get(d_path)
                except OSError:
                    link = None
                if link is not None and not is_safe_symlink_target(
                    d_path, link.target, bucket_root
                ):
                    log.warning(
                        f"{ActionLabel.SECURITY} Symlink target escapes bucket, "
                        f'skipping: "{d_path}" -> "{link.target}"'
                    )
                    continue
                if link is not None:
                    rel = os.path.relpath(d_path, bucket_root)
                    entry = link.to_dict()
                    entry["filepath"] = rel
                    file_list.append(entry)

    # Sort by mtime ascending (oldest first) to prioritize updating older files
    file_list.sort(key=lambda x: x.get("mtime", 0))

    return JSONResponse(
        status_code=200,
        content=file_list,
    )


@auth_router.get("/api/v1/buckets/{bucket}/tombstones")
@limiter.limit(RATE_LIMIT_DEFAULT)
async def get_tombstones(
    request: Request,  # Required by rate limiter
    bucket: str,
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
    if not is_valid_bucket(bucket):
        raise InvalidBucketNameError(details={"bucket": bucket})

    bucket_dir = f"{get_buckets_path()}/{bucket}"
    if not Path(bucket_dir).exists():
        raise BucketNotFoundError(details={"bucket": bucket})

    store = get_tombstone_store(bucket)
    store.prune()

    return JSONResponse(
        status_code=200,
        content=store.to_payload(),
    )


@auth_router.post("/api/v1/buckets/{bucket}/tombstones")
@limiter.limit(RATE_LIMIT_DEFAULT)
async def post_tombstones(
    request: Request,  # Required by rate limiter
    bucket: str,
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
    if not is_valid_bucket(bucket):
        raise InvalidBucketNameError(details={"bucket": bucket})

    bucket_dir = f"{get_buckets_path()}/{bucket}"
    if not Path(bucket_dir).exists():
        raise BucketNotFoundError(details={"bucket": bucket})

    try:
        payload = await request.json()
    except Exception:
        raise InvalidPathError(details={"reason": "invalid JSON body"})
    if not isinstance(payload, dict):
        raise InvalidPathError(details={"reason": "tombstone payload must be a JSON object"})

    store = get_tombstone_store(bucket)
    changed = store.merge(payload)
    if changed:
        store.prune()
        store.save()

    return JSONResponse(status_code=200, content={"merged": changed})
