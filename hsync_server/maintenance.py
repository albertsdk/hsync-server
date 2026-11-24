# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Opt-in storage maintenance shared by the REST delete path and the CLI.

The delete endpoint and ``hsync-server admin maintenance prune-conflicts`` must
remove a path the same way: write the causal tombstone, unlink, drop the hash
and version state, then clean the emptied directory. One implementation means a
maintenance sweep can never desync the state the sync protocol relies on. The
caller passes the bucket directory it already resolved, so this module never
re-derives it.

``restore_entry`` is the inverse and lives here for the same reason: putting a
preserved blob back must clear the tombstone and record a version exactly as a
publish does, or the next sync cycle would treat the restored path as deleted.
"""

from __future__ import annotations

import os
import shutil
import stat as stat_module
import time
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from hsync_common.constants import (
    HISTORY_REASON_DELETE,
    HISTORY_REASON_OVERWRITE,
    PARTIAL_DOWNLOAD_SUFFIX,
)
from hsync_common.hashing import compute_sha256
from hsync_common.models.arbitration import is_conflicted_copy, merge_vclocks, tick_vclock
from hsync_common.models.file import File as LocalFile
from hsync_common.models.file import invalidate_sha256_cache
from hsync_common.models.history import HistoryEntry
from loguru import logger as log

from hsync_server.api.file_utils import prune_empty_dirs
from hsync_server.constants import BACKUP_SUFFIX, ActionLabel
from hsync_server.hashes import set_sha256_hash
from hsync_server.history import capture_file
from hsync_server.history import entry as history_entry
from hsync_server.tombstones import clear_deletion, deletion_vclock, record_path_deletion
from hsync_server.versions import (
    clear_version,
    get_server_cid,
    record_version,
    server_version_info,
)


def delete_path_state(
    bucket: str, abs_path: str, *, bucket_dir: str, cid: str | None
) -> LocalFile | None:
    """Tombstone + unlink + state cleanup for an already-resolved *abs_path*.

    Records the causal deletion marker before unlinking (a crash between the two
    must never resurrect the path), then drops the hash-cache and version entry.
    Returns the deleted ``LocalFile`` (``None`` when it was already gone).
    Synchronous so the admin CLI can call it without an event loop; the REST
    handler broadcasts the ``deleted`` event afterwards.
    """
    if not Path(abs_path).exists():
        return None
    src = LocalFile.get(abs_path)
    # Preserve the bytes before the tombstone makes them unreachable (ADR-0057).
    # A failed capture leaves the deletion intact: the tombstone is what keeps the
    # cluster correct, the copy is only what makes the loss undoable.
    capture_file(bucket, abs_path, bucket_dir=bucket_dir, reason=HISTORY_REASON_DELETE, cid=cid)
    record_path_deletion(
        bucket,
        bucket_dir,
        abs_path,
        sha256sum=src.sha256sum if src else None,
        cid=cid,
    )
    os.remove(abs_path)
    invalidate_sha256_cache(abs_path)
    clear_version(bucket, bucket_dir, abs_path)
    log.info(f'{ActionLabel.DELETE} File: "{abs_path}" deleted successfully (CID: {cid})')
    return src


class RestoreStatus(StrEnum):
    """Outcome of one restore attempt from the history store."""

    RESTORED = "restored"  # the bytes are back at their recorded path
    UNCHANGED = "unchanged"  # the path already holds exactly those bytes
    CONFLICT = "conflict"  # the path is occupied and ``overwrite`` was not asked
    MISSING = "missing"  # the entry or its payload is gone
    DAMAGED = "damaged"  # the payload no longer matches the recorded hash
    FAILED = "failed"  # the filesystem refused the write


@dataclass(frozen=True)
class RestoreOutcome:
    """Result of :func:`restore_entry`, for the CLI to report."""

    status: RestoreStatus
    relpath: str
    abs_path: str
    size: int = 0


def _copy_payload(payload: Path, destination: Path) -> None:
    """Copy *payload* to *destination* through a flushed ``.part`` sibling.

    The rename publishes a complete file, so a listing (which skips ``.part``)
    never sees a half-written restore, and a crash leaves the transient artifact
    rather than a truncated destination.
    """
    partial = destination.with_name(destination.name + PARTIAL_DOWNLOAD_SUFFIX)
    try:
        with payload.open("rb") as reader, partial.open("wb") as writer:
            shutil.copyfileobj(reader, writer, length=1024 * 1024)
            writer.flush()
            os.fsync(writer.fileno())
        os.replace(partial, destination)
    except OSError:
        # The destination is untouched, so the failed attempt must not leave a
        # transient artifact behind for the next sweep to guess about.
        partial.unlink(missing_ok=True)
        raise


def _apply_entry_metadata(destination: Path, entry: HistoryEntry) -> None:
    """Re-apply the recorded mtime and permission bits (best effort).

    The bytes are already in place; a filesystem without POSIX modes (or one
    that refuses the timestamp) must not undo a completed restore.
    """
    if entry["mtime_ns"] > 0:
        try:
            os.utime(destination, ns=(entry["mtime_ns"], entry["mtime_ns"]))
        except OSError:
            pass
    if entry["mode"] and os.name == "posix":
        try:
            os.chmod(destination, entry["mode"])
        except OSError:
            pass


def restore_entry(
    bucket: str,
    entry_id: str,
    *,
    bucket_dir: str,
    overwrite: bool = False,
) -> RestoreOutcome:
    """Put a preserved blob back at the path its record names (ADR-0057).

    A restore is a **local write the server performs on its own behalf**: the
    bytes are published atomically, the deletion marker for the path is cleared
    and a new version is recorded under the server's cid, so the next sync cycle
    sees a live file instead of a tombstone. The entry itself is kept — the
    retention window, not the restore, decides when the copy goes away.

    With ``overwrite=False`` an occupied path is left untouched
    (:attr:`RestoreStatus.CONFLICT`); with ``overwrite=True`` the content being
    replaced is preserved first, exactly like an upload would.
    """
    item = history_entry(bucket, entry_id)
    if item is None:
        return RestoreOutcome(status=RestoreStatus.MISSING, relpath="", abs_path="")
    relpath = item.entry["relpath"]
    destination = Path(bucket_dir) / relpath
    if destination.is_symlink():
        # Never replace a link with a regular file: what the operator would lose
        # is the link itself, which the store cannot give back.
        return RestoreOutcome(
            status=RestoreStatus.CONFLICT,
            relpath=relpath,
            abs_path=str(destination),
            size=item.entry["size"],
        )
    if destination.exists():
        current = LocalFile.get(str(destination))
        if current is not None and current.sha256sum == item.entry["sha256"]:
            clear_deletion(bucket, bucket_dir, str(destination))
            return RestoreOutcome(
                status=RestoreStatus.UNCHANGED,
                relpath=relpath,
                abs_path=str(destination),
                size=item.entry["size"],
            )
        if not overwrite:
            return RestoreOutcome(
                status=RestoreStatus.CONFLICT,
                relpath=relpath,
                abs_path=str(destination),
                size=item.entry["size"],
            )
        capture_file(
            bucket,
            str(destination),
            bucket_dir=bucket_dir,
            reason=HISTORY_REASON_OVERWRITE,
        )
    if not item.payload.is_file():
        return RestoreOutcome(
            status=RestoreStatus.MISSING, relpath=relpath, abs_path=str(destination)
        )
    try:
        intact = item.payload.stat().st_size == item.entry["size"] and (
            compute_sha256(str(item.payload)) == item.entry["sha256"]
        )
        if not intact:
            return RestoreOutcome(
                status=RestoreStatus.DAMAGED,
                relpath=relpath,
                abs_path=str(destination),
                size=item.entry["size"],
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        _copy_payload(item.payload, destination)
        _apply_entry_metadata(destination, item.entry)
    except OSError as exc:
        log.warning(f'history: could not restore "{destination}": {exc}')
        return RestoreOutcome(
            status=RestoreStatus.FAILED, relpath=relpath, abs_path=str(destination)
        )

    # Metadata is final, so the cache can be repopulated (its entry captures the
    # published ``mtime_ns``/``size``); then the marker and the version, or the
    # next cycle would answer the restore with the old deletion. The version
    # clock inherits both the marker's clock and any surviving version clock and
    # is ticked under the server's cid, so the restore is causally *after* the
    # deletion it replaces instead of racing it.
    invalidate_sha256_cache(str(destination))
    set_sha256_hash(str(destination), item.entry["sha256"])
    deleted = deletion_vclock(bucket, bucket_dir, str(destination))
    clear_deletion(bucket, bucket_dir, str(destination))
    server_cid = get_server_cid()
    recorded = server_version_info(bucket, bucket_dir, str(destination))
    record_version(
        bucket,
        bucket_dir,
        str(destination),
        base_hash=item.entry["sha256"],
        vclock=tick_vclock(
            merge_vclocks(recorded["vclock"] if recorded else None, deleted), server_cid
        ),
        origin_cid=server_cid,
        corrected_mtime=item.entry["mtime_ns"] // 1_000_000_000,
    )
    log.info(f'{ActionLabel.CREATE} File: "{destination}" restored from history')
    return RestoreOutcome(
        status=RestoreStatus.RESTORED,
        relpath=relpath,
        abs_path=str(destination),
        size=item.entry["size"],
    )


def cleanup_deleted_path(abs_path: str, *, bucket_dir: str) -> None:
    """Drop the ``.backup`` snapshot and prune the directory the delete vacated."""
    backup_file = f"{abs_path}{BACKUP_SUFFIX}"
    if Path(backup_file).exists():
        os.remove(backup_file)
        log.info(f'{ActionLabel.DELETE} Backup file: "{backup_file}" deleted successfully')
    prune_empty_dirs(str(Path(abs_path).parent), bucket_root=bucket_dir)


def _iter_bucket_files(buckets_root: Path, bucket: str | None) -> Iterator[tuple[str, Path]]:
    """Yield ``(bucket name, path)`` for every file of the selected buckets."""
    names = (
        [bucket]
        if bucket is not None
        else sorted(p.name for p in buckets_root.iterdir() if p.is_dir())
    )
    for name in names:
        root = buckets_root / name
        if not root.is_dir():
            continue
        for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
            for filename in filenames:
                yield name, Path(dirpath) / filename


def prune_conflicted_copies(
    *,
    buckets_root: Path,
    bucket: str | None = None,
    older_than_s: float,
    dry_run: bool = False,
    now: float | None = None,
) -> list[Path]:
    """Delete conflicted-copy siblings older than *older_than_s*; return them.

    Opt-in and targeted: only basenames matching the canonical
    ``_conflicted_copy_<ts>_`` marker (``is_conflicted_copy``) are eligible, only
    regular files (a symlink is never followed or removed), and only those whose
    mtime is at least *older_than_s* in the past. Each removal goes through
    ``delete_path_state`` + ``cleanup_deleted_path`` so tombstones and version
    state stay consistent. ``dry_run`` reports the targets without touching them.
    """
    moment = time.time() if now is None else now
    cutoff = moment - older_than_s
    removed: list[Path] = []
    for name, path in _iter_bucket_files(buckets_root, bucket):
        if not is_conflicted_copy(path.name):
            continue
        try:
            st = path.lstat()
        except OSError:
            continue
        if not stat_module.S_ISREG(st.st_mode) or st.st_mtime > cutoff:
            continue
        removed.append(path)
        if dry_run:
            continue
        bucket_dir = str(buckets_root / name)
        delete_path_state(name, str(path), bucket_dir=bucket_dir, cid=None)
        cleanup_deleted_path(str(path), bucket_dir=bucket_dir)
    return removed


def prune_orphaned_backups(
    *,
    buckets_root: Path,
    bucket: str | None = None,
    older_than_s: float,
    dry_run: bool = False,
    now: float | None = None,
) -> list[Path]:
    """Delete ``<path>.backup`` snapshots that outlived their publish.

    An overwrite snapshots the previous bytes to ``<path>.backup`` and removes the
    snapshot once the new content is published, so one still present after
    *older_than_s* was left by a publish interrupted after that rename. Such a
    snapshot is **kept while its primary is missing**: ``download.py`` serves it
    as the last complete copy, so deleting it would destroy the only data the
    crash left behind.

    The suffix is hidden from listings and is not a synced path, so the removal
    takes no tombstone — only the file. ``dry_run`` reports the targets without
    touching them.
    """
    moment = time.time() if now is None else now
    cutoff = moment - older_than_s
    removed: list[Path] = []
    for _bucket, path in _iter_bucket_files(buckets_root, bucket):
        filename = path.name
        if not filename.endswith(BACKUP_SUFFIX):
            continue
        if not path.with_name(filename[: -len(BACKUP_SUFFIX)]).exists():
            continue
        try:
            st = path.lstat()
        except OSError:
            continue
        if not stat_module.S_ISREG(st.st_mode) or st.st_mtime > cutoff:
            continue
        if not dry_run:
            try:
                path.unlink()
            except OSError:
                continue
            # A snapshot is hashed when it is served as the fallback copy.
            invalidate_sha256_cache(str(path))
        removed.append(path)
    return removed
