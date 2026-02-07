# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
Per-bucket version store for HSYNC Server.

Wraps :class:`hsync_common.models.version.SyncStateStore` with one persisted
store per bucket, backed by the installation ``syncstate.db`` and keyed by a
folder UUID in the ``buckets`` table (ADR-0025). No marker is written into
served bucket directories. The server records the causal version of every
file it accepts on upload, so its listings and events can advertise version
metadata and its upload arbitration can compare incoming versions causally
(ADR-0022).

The connection is opened lazily on first request (never at import) so a
uvicorn multi-worker fork does not inherit a parent's SQLite handle. A
per-bucket :class:`threading.RLock` serializes mutations.

The server is not an author of content, but out-of-band edits on its disk
(third-party writes to the bucket directory) are detected at arbitration time
(disk hash ≠ stored base_hash) and self-recorded with a **reserved server node
cid** — a stable 21-char id minted once and persisted next to the DB.
"""

from __future__ import annotations

import os
import secrets
import threading
from pathlib import Path

from hsync_common.constants import SYNCSTATE_DB_FILENAME
from hsync_common.models.sqlstore import get_state_db, reset_state_db_cache
from hsync_common.models.version import SyncStateStore, relpath_in_bucket

from hsync_server.config import get_state_dir

__all__ = [
    "clear_version",
    "get_server_cid",
    "get_version_store",
    "move_version",
    "record_version",
    "reset_cache",
    "server_version_info",
]

_stores: dict[str, SyncStateStore] = {}
_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()
_pid: int | None = None

_SERVER_CID_FILENAME = ".hsyncnodeid"


def _rebind_after_fork() -> None:
    """Drop inherited SQLite handles after a worker fork."""
    global _pid
    current = os.getpid()
    if _pid is None:
        _pid = current
        return
    if _pid != current:
        _stores.clear()
        _locks.clear()
        reset_state_db_cache()
        _pid = current


def _bucket_lock(bucket: str) -> threading.RLock:
    with _locks_guard:
        lock = _locks.get(bucket)
        if lock is None:
            lock = threading.RLock()
            _locks[bucket] = lock
        return lock


def _db_path() -> str:
    return str(Path(get_state_dir()) / SYNCSTATE_DB_FILENAME)


def get_version_store(bucket: str) -> SyncStateStore:
    """Return the cached store for a bucket, loading it on first use."""
    _rebind_after_fork()
    store = _stores.get(bucket)
    if store is None:
        db_path = _db_path()
        folder_id = get_state_db(db_path).ensure_bucket_id(bucket)
        store = SyncStateStore(
            db_path=db_path,
            folder_id=folder_id,
            canonical_path=f"bucket:{bucket}",
        )
        store.load()
        _stores[bucket] = store
    return store


def record_version(
    bucket: str,
    bucket_dir: str,
    abs_path: str,
    base_hash: str,
    vclock: dict[str, int] | None = None,
    version_id: str | None = None,
    origin_cid: str | None = None,
    corrected_mtime: int = 0,
) -> None:
    """Record (overwrite) the version for an absolute path and persist the store.

    ``corrected_mtime`` is the ADR-0023 offset-corrected modification time of
    the content; 0 ⇒ unknown and is stored verbatim (the hash fallback applies
    on comparison).
    """
    relpath = relpath_in_bucket(abs_path, bucket_dir)
    if not relpath:
        return
    with _bucket_lock(bucket):
        store = get_version_store(bucket)
        store.record(
            relpath,
            base_hash=base_hash,
            vclock=vclock,
            version_id=version_id,
            origin_cid=origin_cid,
            corrected_mtime=corrected_mtime,
        )
        store.save()


def clear_version(bucket: str, bucket_dir: str, abs_path: str) -> None:
    """Drop the version entry for a path (e.g. on deletion) and persist."""
    relpath = relpath_in_bucket(abs_path, bucket_dir)
    if not relpath:
        return
    with _bucket_lock(bucket):
        store = get_version_store(bucket)
        if store.clear(relpath):
            store.save()


def move_version(bucket: str, bucket_dir: str, src_abs: str, dst_abs: str) -> None:
    """Move the version entry from src to dst (rename) and persist.

    A rename is not a content edit: the hash and clock are carried over
    unchanged (no tick).
    """
    src_rel = relpath_in_bucket(src_abs, bucket_dir)
    dst_rel = relpath_in_bucket(dst_abs, bucket_dir)
    if not src_rel or not dst_rel:
        return
    with _bucket_lock(bucket):
        store = get_version_store(bucket)
        entry = store.get(src_rel)
        if entry is None:
            return
        store.clear(src_rel)
        store.record(
            dst_rel,
            base_hash=entry["base_hash"],
            vclock=entry["vclock"],
            version_id=entry["version_id"],
            origin_cid=entry["origin_cid"],
            timestamp=entry["ts"],
            corrected_mtime=entry.get("corrected_mtime", 0),
        )
        store.save()


def server_version_info(bucket: str, bucket_dir: str, abs_path: str):
    """Return the :class:`VersionInfo` for a path, or None if unrecorded."""
    from hsync_common.models.version import VersionInfo

    relpath = relpath_in_bucket(abs_path, bucket_dir)
    if not relpath:
        return None
    with _bucket_lock(bucket):
        entry = get_version_store(bucket).get(relpath)
    if entry is None:
        return None
    return VersionInfo(
        vclock=entry["vclock"],
        version_id=entry["version_id"],
        origin_cid=entry["origin_cid"],
        ts=entry["ts"],
        corrected_mtime=entry.get("corrected_mtime", 0),
    )


def get_server_cid() -> str:
    """Return the server's reserved node cid, minting and persisting it once.

    The cid is a 21-char alphanumeric string (``is_valid_cid``-compliant) used
    solely as the author of out-of-band edits detected at arbitration time.
    Persisted next to ``syncstate.db`` (outside any served bucket).
    """
    cid_path = Path(get_state_dir()) / _SERVER_CID_FILENAME
    if cid_path.exists():
        cid = cid_path.read_text(encoding="utf-8").strip()
        if len(cid) == 21 and cid.isalnum():
            return cid
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    cid = "".join(alphabet[b // 4 % len(alphabet)] for b in secrets.token_bytes(21))
    cid_path.parent.mkdir(parents=True, exist_ok=True)
    cid_path.write_text(cid, encoding="utf-8")
    return cid


def reset_cache() -> None:
    """Clear the in-memory store cache (primarily for tests)."""
    _stores.clear()
    _locks.clear()
