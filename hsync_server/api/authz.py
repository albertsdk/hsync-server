# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
Shared authorization helper for bucket-scoped REST endpoints.

``check_bucket_access`` is the single source of truth for the imperative
"caller may access this bucket" check used by the file, tombstone, symlink,
delta and bucket-management endpoints. It raises:

- ``InvalidBucketNameError`` (400) when the bucket name is malformed.
- ``BucketNotFoundError`` (404) when the bucket directory does not exist
  **or** the authenticated account is neither admin nor a member — the two
  cases are deliberately indistinguishable so the endpoint never leaks the
  existence of a bucket the caller cannot access.

The check is intended to run as the first statement of an endpoint body,
before any request data is read or filesystem work is performed, so an
unauthorized caller is rejected with no side effects. The caller obtains
``ctx`` from the ``verify_api_key`` FastAPI dependency (already enforced
router-wide on ``auth_router``); this helper only enforces data-level
bucket authorization.
"""

from pathlib import Path

from hsync_server.api.utils import is_valid_bucket
from hsync_server.auth import AuthContext
from hsync_server.auth_store import get_auth_store
from hsync_server.config import get_buckets_path
from hsync_server.exceptions import BucketNotFoundError, InvalidBucketNameError

__all__ = ["check_bucket_access", "ensure_bucket_access"]


def check_bucket_access(bucket: str, ctx: AuthContext) -> None:
    """Raise 404 if the caller cannot access *bucket* (or it doesn't exist).

    Returns ``None`` on success.  A nonexistent bucket and a bucket the
    caller has no membership on both map to ``BucketNotFoundError`` (404) so
    that existence is never leaked; a malformed name is a 400.
    """
    if not is_valid_bucket(bucket):
        raise InvalidBucketNameError(details={"bucket": bucket})
    bucket_dir = Path(get_buckets_path()) / bucket
    if not bucket_dir.exists():
        raise BucketNotFoundError(details={"bucket": bucket})
    store = get_auth_store()
    if not store.has_access(ctx.id, bucket, is_admin=(ctx.role == "admin")):
        # 404 to avoid leaking existence.
        raise BucketNotFoundError(details={"bucket": bucket})


def ensure_bucket_access(bucket: str, ctx: AuthContext) -> None:
    """Auto-create *bucket* if absent (caller becomes owner), then check access.

    Used by the upload endpoint, which has historically created the bucket
    directory on first upload.  If the directory already exists, this is
    equivalent to ``check_bucket_access``.
    """
    from shutil import rmtree

    if not is_valid_bucket(bucket):
        raise InvalidBucketNameError(details={"bucket": bucket})
    bucket_dir = Path(get_buckets_path()) / bucket
    if not bucket_dir.exists():
        # Auto-create the bucket and set ownership.
        try:
            bucket_dir.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            pass  # Race — another worker created it; fall through to access check.
        store = get_auth_store()
        try:
            store.set_owner_on_create(bucket, ctx.id)
        except Exception:
            # INSERT failed after mkdir — remove the orphan and error.
            try:
                rmtree(bucket_dir)
            except OSError:
                pass
            raise
    # Bucket exists — check access.
    check_bucket_access(bucket, ctx)
