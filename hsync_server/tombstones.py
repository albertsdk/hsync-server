# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
Per-bucket tombstone management for HSYNC Server.

Wraps :class:`hsync_common.models.tombstone.TombstoneStore` with one persisted
store per bucket, backed by a hidden side-car file at the bucket root. Deletion
markers let a client that was offline at deletion time avoid resurrecting the
file during a full reconciliation.
"""

from hsync_common.constants import HSYNCTOMBSTONES_FILENAME
from hsync_common.models.tombstone import TombstoneStore

from hsync_server.config import get_buckets_path, get_tombstone_ttl_days

# Cache of per-bucket stores, keyed by bucket name.
_stores: dict[str, TombstoneStore] = {}


def _store_path(bucket: str) -> str:
    """Return the side-car file path for a bucket's tombstones."""
    return f"{get_buckets_path()}/{bucket}/{HSYNCTOMBSTONES_FILENAME}"


def relpath_in_bucket(abs_path: str, bucket_dir: str) -> str:
    """Return the bucket-relative path used as the tombstone identity."""
    return str(abs_path).removeprefix(str(bucket_dir)).lstrip("/")


def get_tombstone_store(bucket: str) -> TombstoneStore:
    """Return the cached store for a bucket, loading and pruning it on first use."""
    store = _stores.get(bucket)
    if store is None:
        store = TombstoneStore(path=_store_path(bucket), ttl_days=get_tombstone_ttl_days())
        store.load()
        store.prune()
        _stores[bucket] = store
    return store


def record_deletion(
    bucket: str,
    bucket_dir: str,
    abs_path: str,
    sha256sum: str | None = None,
    timestamp: int | float | None = None,
) -> None:
    """Record a deletion marker for an absolute path and persist the store."""
    relpath = relpath_in_bucket(abs_path, bucket_dir)
    if not relpath:
        return
    store = get_tombstone_store(bucket)
    store.record(relpath, sha256sum=sha256sum, timestamp=timestamp)
    store.prune()
    store.save()


def clear_deletion(bucket: str, bucket_dir: str, abs_path: str) -> None:
    """Drop the deletion marker for a re-created/uploaded path and persist."""
    relpath = relpath_in_bucket(abs_path, bucket_dir)
    if not relpath:
        return
    store = get_tombstone_store(bucket)
    if store.clear(relpath):
        store.save()


def reset_cache() -> None:
    """Clear the in-memory store cache (primarily for tests)."""
    _stores.clear()
