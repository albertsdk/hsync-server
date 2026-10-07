# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""
Bucket provisioning: create/delete bucket directories and record ownership.

This module is the single path for bucket creation and deletion used by both
the REST endpoints (``hsync_server.api.buckets``) and the local admin CLI
(``hsync_server.__main__`` → ``admin bucket create`` / ``admin bucket delete``),
so the filesystem, ownership and cleanup rules cannot drift between the two
interfaces.
"""

from __future__ import annotations

from pathlib import Path

from hsync_common.path_validation import is_valid_bucket_name
from loguru import logger as log

from hsync_server.auth_store import get_auth_store
from hsync_server.config import get_buckets_path
from hsync_server.exceptions import (
    BucketNotFoundError,
    ConflictError,
    DeleteError,
    InvalidBucketNameError,
)

__all__ = ["create_bucket", "delete_bucket"]


def create_bucket(bucket: str, *, owner_account_id: str, owner_name: str) -> str:
    """Create a bucket directory and assign its owner atomically.

    Args:
        bucket: Bucket name (validated against the shared SSOT rules).
        owner_account_id: Account id that becomes the owner.
        owner_name: Owner account name (for logging only).

    Returns:
        The created bucket's name.

    Raises:
        InvalidBucketNameError: The bucket name is invalid.
        ConflictError: A bucket (directory) with the same name already exists.
        DeleteError: The directory was created but ownership could not be
            recorded; the orphan directory is removed before raising.
    """
    if not is_valid_bucket_name(bucket):
        raise InvalidBucketNameError(details={"bucket": bucket})

    bucket_directory = Path(get_buckets_path()) / bucket

    try:
        bucket_directory.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        log.warning(f"Bucket already exists: {bucket_directory}")
        raise ConflictError(f"Bucket '{bucket}' already exists.") from None

    store = get_auth_store()
    try:
        store.set_owner_on_create(bucket, owner_account_id)
    except Exception:
        # INSERT failed after mkdir — remove the orphan directory.
        from shutil import rmtree

        log.error(f"Failed to record ownership for bucket '{bucket}'", exc_info=True)
        try:
            rmtree(bucket_directory)
        except OSError:
            pass  # Best-effort cleanup.
        raise DeleteError(
            message=f"Failed to create bucket: {bucket}",
            details={"bucket": bucket},
        )

    log.info(f'Bucket: "{bucket}" created (owner: {owner_name})')
    return bucket


def delete_bucket(bucket: str, *, delete_files: bool = True) -> str:
    """Delete a bucket: DB rows and, by default, its filesystem directory.

    Args:
        bucket: Bucket name.
        delete_files: When False, only the DB rows are removed (cleanup of
            "ghost" buckets whose directory no longer exists on disk). The
            REST endpoint always uses the default ``True``.

    Returns:
        The deleted bucket's name.

    Raises:
        InvalidBucketNameError: The bucket name is invalid.
        BucketNotFoundError: ``delete_files`` is True and no directory exists
            for the bucket (checked before and, on removal failure, after, so a
            concurrent delete that wins the race is a 404 rather than a 500).
        DeleteError: The directory could not be removed.
    """
    if not is_valid_bucket_name(bucket):
        raise InvalidBucketNameError(details={"bucket": bucket})

    bucket_dir = Path(get_buckets_path()) / bucket
    if delete_files and not bucket_dir.exists():
        raise BucketNotFoundError(details={"bucket": bucket})

    store = get_auth_store()
    if delete_files:
        try:
            store.delete_bucket(bucket)
        except OSError as e:
            # Another delete can win the race after the exists() check above
            # (the REST endpoint yields while this runs on a worker thread):
            # a now-missing directory is a 404, not a server error.
            if not bucket_dir.exists():
                raise BucketNotFoundError(details={"bucket": bucket}) from None
            log.error(f"Failed to delete bucket {bucket}: {e}", exc_info=True)
            raise DeleteError(
                message=f"Failed to delete bucket: {bucket}",
                details={"bucket": bucket, "error": str(e)},
            ) from None
    else:
        store.delete_bucket_db(bucket)

    log.info(f'Bucket: "{bucket}" deleted (files={"yes" if delete_files else "db-only"})')
    return bucket
