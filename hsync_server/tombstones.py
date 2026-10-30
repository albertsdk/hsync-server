# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""
Per-bucket tombstone management for HSYNC Server.

Wraps :class:`hsync_common.models.tombstone.TombstoneStore` with one persisted
store per bucket, backed by the installation ``state DB`` and keyed by a
folder UUID in the ``buckets`` table (ADR-0025). No marker is written into
served bucket directories. Deletion markers let a client that was offline at
deletion time avoid resurrecting the file during a full reconciliation.
"""

from __future__ import annotations

from os import walk
from pathlib import Path

from hsync_common.models.event import is_valid_cid
from hsync_common.models.file import File
from hsync_common.models.tombstone import TombstoneStore, relpath_in_bucket
from loguru import logger as log

from hsync_server.api.file_utils import is_sentinel_file
from hsync_server.config import get_tombstone_ttl_days
from hsync_server.store_registry import PerBucketStores, bucket_state_identity
from hsync_server.versions import clear_version, get_version_store

__all__ = [
    "clear_deletion",
    "clear_deletion_tree",
    "deletion_marker_snapshot",
    "destination_mutation_since",
    "get_tombstone_store",
    "record_deletion",
    "record_deletion_tree",
    "record_path_deletion",
    "reset_cache",
]

# Fork-safe, thread-safe per-bucket registry (shared scaffolding with
# ``versions.py``; see ``store_registry.PerBucketStores``).
_registry: PerBucketStores[TombstoneStore] = PerBucketStores()


def get_tombstone_store(bucket: str) -> TombstoneStore:
    """Return the cached store for a bucket, loading and pruning it on first use."""

    def _build() -> TombstoneStore:
        db_path, folder_id = bucket_state_identity(bucket)
        store = TombstoneStore(
            db_path=db_path,
            folder_id=folder_id,
            ttl_days=get_tombstone_ttl_days(),
            canonical_path=f"bucket:{bucket}",
        )
        store.load()
        store.prune()
        return store

    return _registry.get_or_create(bucket, _build)


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
    with _registry.lock(bucket):
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
    with _registry.lock(bucket):
        store = get_tombstone_store(bucket)
        if store.clear(relpath):
            store.save()


def clear_deletion_tree(bucket: str, bucket_dir: str, abs_dir: str) -> None:
    """Drop deletion markers for a directory and every file beneath it.

    Used after a rename repopulates a destination tree: an earlier delete may
    have left markers for the same relative paths, which would otherwise make
    peers treat the restored content as deleted.
    """
    clear_deletion(bucket, bucket_dir, abs_dir)
    for root, _dirs, files in walk(abs_dir):
        for name in files:
            clear_deletion(bucket, bucket_dir, str(Path(root) / name))


def record_deletion_tree(
    bucket: str,
    bucket_dir: str,
    abs_dir: str,
    *,
    cid: str | None = None,
    clear_versions: bool = True,
) -> int:
    """Mint a causal tombstone for every file under *abs_dir*; return the count.

    A recursive directory operation replaces one event per descendant, so the
    descendants' markers must be recorded here — the single-path
    :func:`record_path_deletion` never runs for them. Directories carry no
    tombstones, sentinels are per-peer and never minted, and each call inherits
    the stored version clock exactly like a single-path deletion (ADR-0003).

    ``clear_versions`` distinguishes the two callers: a deletion drops each
    file's version entry (a re-created path starts fresh), while a rename must
    keep them — ownership moves to the destination through
    :func:`hsync_server.versions.move_version_prefix`.

    Best-effort by contract: a failure to record one marker must never abort
    the surrounding filesystem operation.
    """
    recorded = 0
    for root, _dirs, files in walk(abs_dir):
        for name in files:
            child = str(Path(root) / name)
            try:
                child_obj = File.get(child)
                sha256sum = child_obj.sha256sum if child_obj else None
            except OSError:
                sha256sum = None
            try:
                if record_path_deletion(bucket, bucket_dir, child, sha256sum=sha256sum, cid=cid):
                    if clear_versions:
                        clear_version(bucket, bucket_dir, child)
                    recorded += 1
            except Exception as e:  # pragma: no cover - best-effort bookkeeping
                log.debug(f"Could not record tombstone for {child}: {e}")
    return recorded


def reset_cache() -> None:
    """Clear the in-memory store cache (primarily for tests)."""
    _registry.clear()


def record_path_deletion(
    bucket: str,
    bucket_dir: str,
    abs_path: str,
    *,
    sha256sum: str | None = None,
    cid: str | None = None,
) -> bool:
    """Record (or clear) the causal deletion marker for one path.

    Single definition of the delete bookkeeping shared by the REST delete
    endpoint and the ``deleted`` event handlers:

    - a *sentinel* helper is per-peer and never exchanged, so any stale marker
      is dropped (:func:`clear_deletion`) and no tombstone is minted;
    - for a real path the stored version clock is inherited and ticked by the
      deleting peer, so delete-vs-recreate and delete-vs-delete resolve
      causally (ADR-0003).

    Returns True when a tombstone was recorded. Callers that must drop the
    causal version entry do so themselves: its ordering relative to the
    filesystem unlink differs between the REST delete path and the handlers.
    """
    if is_sentinel_file(abs_path):
        clear_deletion(bucket, bucket_dir, abs_path)
        return False
    relpath = relpath_in_bucket(abs_path, bucket_dir)
    if not relpath:
        return False
    stored = get_version_store(bucket).get(relpath)
    record_deletion(
        bucket,
        bucket_dir,
        abs_path,
        sha256sum=sha256sum,
        origin_cid=cid if cid and is_valid_cid(cid) else None,
        prev_vclock=stored["vclock"] if stored else None,
    )
    return True


def deletion_marker_snapshot(
    bucket: str,
    bucket_dir: str,
    abs_path: str,
) -> tuple[str, tuple[tuple[str, int], ...]] | None:
    """Opaque snapshot of the deletion marker for *abs_path* (None when absent).

    Every mint, merge and clear changes the marker's ``(version_id, vclock)``,
    so holding the snapshot across an operation and comparing it afterwards
    detects that the path was deleted, renamed away or re-created while the
    operation ran (same contract as the client helper of the same name).
    """
    relpath = relpath_in_bucket(abs_path, bucket_dir)
    if not relpath:
        return None
    with _registry.lock(bucket):
        entry = get_tombstone_store(bucket).get(relpath)
    if entry is None:
        return None
    return (entry["version_id"], tuple(sorted(entry["vclock"].items())))


def destination_mutation_since(
    bucket: str,
    bucket_dir: str,
    abs_path: str,
    existed_at_start: bool,
    marker_snapshot: tuple[str, tuple[tuple[str, int], ...]] | None,
) -> bool:
    """True when *abs_path* was removed or replaced since the snapshot was taken.

    The destination changed under a transfer that is about to commit over it: a
    delete or rename-away is caught by the presence check (the case even a run
    without a watcher can see), any deletion-marker change — including a
    delete-and-recreate — by the snapshot comparison.
    """
    if existed_at_start and not Path(abs_path).exists():
        return True
    return deletion_marker_snapshot(bucket, bucket_dir, abs_path) != marker_snapshot
