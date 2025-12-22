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
from hsync_common.models.tombstone import TombstoneStore, relpath_in_bucket

from hsync_server.config import get_buckets_path, get_tombstone_ttl_days

__all__ = [
    "clear_deletion",
    "get_tombstone_store",
    "record_deletion",
    "relpath_in_bucket",
    "reset_cache",
]

# Cache of per-bucket stores, keyed by bucket name.
_stores: dict[str, TombstoneStore] = {}


def _store_path(bucket: str) -> str:
    """Return the side-car file path for a bucket's tombstones."""
    return f"{get_buckets_path()}/{bucket}/{HSYNCTOMBSTONES_FILENAME}"


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
    origin_cid: str | None = None,
    prev_vclock: dict[str, int] | None = None,
    timestamp: int | float | None = None,
) -> None:
    """Record a causal deletion marker for an absolute path and persist it.

    The marker inherits the stored version clock for the path (``prev_vclock``)
    and is ticked by ``origin_cid``, so delete-vs-recreate and delete-vs-delete
    resolve causally (ADR-0003) — the deletion timestamp is informational only.
    """
    relpath = relpath_in_bucket(abs_path, bucket_dir)
    if not relpath:
        return
    store = get_tombstone_store(bucket)
    store.record(
        relpath,
        sha256sum=sha256sum,
        origin_cid=origin_cid,
        prev_vclock=prev_vclock,
        timestamp=timestamp,
    )
    store.prune()
    store.save()


def clear_deletion(bucket: str, bucket_dir: str, abs_path: str) -> None:
    """Drop the deletion marker for a re-created/uploaded path and persist.

    A re-created file carries a clock that supersedes the tombstone clock, so
    the marker is removed (never a stale deletion for a live file).
    """
    relpath = relpath_in_bucket(abs_path, bucket_dir)
    if not relpath:
        return
    store = get_tombstone_store(bucket)
    if store.clear(relpath):
        store.save()


def reset_cache() -> None:
    """Clear the in-memory store cache (primarily for tests)."""
    _stores.clear()
