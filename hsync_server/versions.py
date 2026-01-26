# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
Per-bucket version store for HSYNC Server.

Wraps :class:`hsync_common.models.version.SyncStateStore` with one persisted
store per bucket, backed by a hidden ``.hsyncstate`` side-car at the bucket
root (excluded from sync via :func:`hsync_common.path_validation.is_internal_name`,
same precedent as ``.hsynctombstones``). The server records the causal version
of every file it accepts on upload, so its listings and events can advertise
version metadata and its upload arbitration can compare incoming versions
causally (ADR-0022).

The server is not an author of content, but out-of-band edits on its disk
(third-party writes to the bucket directory) are detected at arbitration time
(disk hash ≠ stored base_hash) and self-recorded with a **reserved server node
cid** — a stable 21-char id minted once and persisted outside the synced tree.
"""

import secrets
from pathlib import Path

from hsync_common.constants import HSYNCSTATE_FILENAME
from hsync_common.models.version import SyncStateStore, new_version_id, relpath_in_bucket

from hsync_server.config import get_buckets_path

__all__ = [
    "clear_version",
    "get_server_cid",
    "get_version_store",
    "move_version",
    "record_version",
    "reset_cache",
    "server_version_info",
]

# Cache of per-bucket stores, keyed by bucket name.
_stores: dict[str, SyncStateStore] = {}

# Path to the persisted reserved server node id (outside the synced tree:
# the buckets root itself is never listed, only its bucket subdirectories).
_SERVER_CID_FILENAME = ".hsyncnodeid"


def _store_path(bucket: str) -> str:
    """Return the side-car file path for a bucket's version store."""
    return f"{get_buckets_path()}/{bucket}/{HSYNCSTATE_FILENAME}"


def get_version_store(bucket: str) -> SyncStateStore:
    """Return the cached store for a bucket, loading it on first use."""
    store = _stores.get(bucket)
    if store is None:
        store = SyncStateStore(path=_store_path(bucket))
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
    Persisted at the buckets root (outside any synced bucket subdirectory).
    """
    cid_path = Path(get_buckets_path()) / _SERVER_CID_FILENAME
    if cid_path.exists():
        cid = cid_path.read_text(encoding="utf-8").strip()
        if len(cid) == 21 and cid.isalnum():
            return cid
    # Mint a fresh 21-char alphanumeric id (base62 from secrets).
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    cid = "".join(alphabet[b // 4 % len(alphabet)] for b in secrets.token_bytes(21))
    cid_path.parent.mkdir(parents=True, exist_ok=True)
    cid_path.write_text(cid, encoding="utf-8")
    return cid


def reset_cache() -> None:
    """Clear the in-memory store cache (primarily for tests)."""
    _stores.clear()
