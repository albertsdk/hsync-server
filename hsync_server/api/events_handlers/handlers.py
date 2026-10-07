# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""
WebSocket event handlers for HSYNC Server.

This module provides handler functions for different event types:
- handle_added_event: Create new files/directories
- handle_modified_event: Update existing files/directories
- handle_deleted_event: Remove files/directories
- handle_renamed_event: Rename/move files or directories
- handle_symlink_event: Create or update symbolic links
"""

from os import remove, rename, unlink, walk
from os import symlink as os_symlink
from pathlib import Path
from shutil import rmtree
from typing import Any

from hsync_common.models.dir import Dir
from hsync_common.models.event import Event, EventName
from hsync_common.models.file import File
from hsync_common.models.symlink import Symlink
from hsync_common.path_validation import is_safe_symlink_target
from loguru import logger as log

from hsync_server.api.events_handlers.validation import (
    is_path_within_bucket,
)
from hsync_server.api.file_utils import (
    cleanup_empty_dir,
    ensure_sentinel_in_dir,
    is_sentinel_file,
)
from hsync_server.api.utils import set_directory_permissions
from hsync_server.constants import ActionLabel
from hsync_server.hashes import del_sha256_hash
from hsync_server.tombstones import clear_deletion, clear_deletion_tree, record_path_deletion
from hsync_server.versions import clear_version, move_version, move_version_prefix

# ---------------------------------------------------------------------------
# Shared validation helpers (DRY: replaces 8 duplicated path-validation blocks)
# ---------------------------------------------------------------------------


def _validate_path(path: str | None, bucket_dir: str, *, allow_root: bool = True) -> bool:
    """Return False when *path* is missing or escapes *bucket_dir*, logging the attempt."""
    if path is None or not is_path_within_bucket(path, bucket_dir, allow_root=allow_root):
        log.warning(f"{ActionLabel.SECURITY} Rejected path traversal attempt: {path!r}")
        return False
    return True


# ---------------------------------------------------------------------------
# Shared create-or-modify handler (DRY: added + modified were ~90% identical)
# ---------------------------------------------------------------------------


async def _handle_create_or_modify(
    src: dict[str, Any] | None,
    bucket_dir: str,
    bucket: str,
    cid: str,
    event_name: EventName,
    *,
    create_dir: bool = False,
) -> Event | None:
    """Common logic for ``added`` and ``modified`` events."""
    if not src:
        return None

    label = ActionLabel.CREATE if create_dir else ActionLabel.MODIFY
    verb = "Added" if create_dir else "Modified"

    if "path" in src:
        dir_obj = Dir.from_dict(src, bucket_dir)
        if not _validate_path(dir_obj.path, bucket_dir):
            return None
        if dir_obj.path and dir_obj.permissions:
            log.info(f'{label} {verb} dir "{dir_obj.path}" (CID: {cid})')
            if create_dir:
                Path(dir_obj.path).mkdir(parents=True, exist_ok=True)
            await set_directory_permissions(dir_obj.path, dir_obj.permissions)
            return Event(cid, bucket_dir, bucket, event_name, dir_obj)

    elif "filepath" in src:
        file_obj = File.from_dict(src, bucket_dir, False)
        filepath = file_obj.filepath
        if filepath is None or not _validate_path(filepath, bucket_dir):
            return None
        log.info(f'{label} {verb} file "{filepath}" (CID: {cid})')
        # A (re-)created file invalidates any prior deletion marker so peers
        # do not delete the freshly added file on their next full sync.
        if not is_sentinel_file(filepath):
            clear_deletion(bucket, bucket_dir, filepath)
        return Event(cid, bucket_dir, bucket, event_name, file_obj)

    return None


# ---------------------------------------------------------------------------
# Public event handlers
# ---------------------------------------------------------------------------


async def handle_added_event(
    src: dict[str, Any] | None,
    bucket_dir: str,
    bucket: str,
    cid: str,
) -> Event | None:
    """Handle 'added' events — create new files/directories."""
    return await _handle_create_or_modify(
        src,
        bucket_dir,
        bucket,
        cid,
        EventName.added,
        create_dir=True,
    )


async def handle_modified_event(
    src: dict[str, Any] | None,
    bucket_dir: str,
    bucket: str,
    cid: str,
) -> Event | None:
    """Handle 'modified' events — update existing files/directories."""
    return await _handle_create_or_modify(
        src,
        bucket_dir,
        bucket,
        cid,
        EventName.modified,
        create_dir=False,
    )


def _record_tombstones_for_tree(dir_path: str, bucket_dir: str, bucket: str, cid: str) -> int:
    """Mint causal tombstones for every file under *dir_path*.

    A recursive ``deleted`` event for a directory replaces one delete event
    per descendant, so the descendants' deletion markers must be recorded
    here — the per-file branch of :func:`handle_deleted_event` never runs for
    them. Mirrors that branch exactly: inherit the stored version clock, tick
    by the deleting peer, drop the version entry; sentinels are per-peer
    helpers and are never minted. Best-effort by contract: a failure to
    record a marker must never abort the ``rmtree``.
    """
    recorded = 0
    for root, _dirs, files in walk(dir_path):
        for name in files:
            child = str(Path(root) / name)
            try:
                child_obj = File.get(child)
                sha256sum = child_obj.sha256sum if child_obj else None
            except OSError:
                sha256sum = None
            try:
                if record_path_deletion(bucket, bucket_dir, child, sha256sum=sha256sum, cid=cid):
                    clear_version(bucket, bucket_dir, child)
                    recorded += 1
            except Exception as e:  # pragma: no cover - best-effort bookkeeping
                log.debug(f"Could not record tombstone for {child}: {e}")
    return recorded


async def handle_deleted_event(
    src: dict[str, Any] | None,
    bucket_dir: str,
    bucket: str,
    cid: str,
) -> Event | None:
    """
    Handle 'deleted' events - remove files/directories.

    Args:
        src: Source object data from the event.
        bucket_dir: Full path to the bucket directory.
        bucket: Bucket name.
        cid: Client ID.

    Returns:
        Event object if successful, None otherwise.
    """
    if not src:
        return None

    if "path" in src:
        dir_obj = Dir.from_dict(src, bucket_dir)
        # allow_root=False rejects an attempt to rmtree the bucket directory itself
        if not _validate_path(dir_obj.path, bucket_dir, allow_root=False):
            return None
        if dir_obj.path:
            if not Path(dir_obj.path).exists():
                log.warning(f'{ActionLabel.DELETE} Dir: "{dir_obj.path}" not found.')
            else:
                # A recursive directory event replaces the per-descendant
                # delete stream: record the descendants' causal tombstones
                # before the tree disappears (ADR-0003). Tombstone walk and
                # rmtree stay synchronous ON PURPOSE: offloading them would
                # let uploads publish new descendants into the tree mid-walk
                # that the rmtree then removes without a tombstone — a
                # resurrection regression. Full loop-freedom requires a
                # shared bucket/tree mutation lock (documented follow-up).
                _record_tombstones_for_tree(dir_obj.path, bucket_dir, bucket, cid)
                log.info(f'{ActionLabel.DELETE} Deleted dir "{dir_obj.path}" (CID: {cid})')
                rmtree(dir_obj.path, ignore_errors=False)
            return Event(cid, bucket_dir, bucket, EventName.deleted, dir_obj)

    elif "filepath" in src:
        file_obj = File.from_dict(src, bucket_dir, False)
        if not _validate_path(file_obj.filepath, bucket_dir):
            return None
        if file_obj.filepath:
            is_sentinel = is_sentinel_file(file_obj.filepath)
            if not Path(file_obj.filepath).exists():
                log.warning(f'{ActionLabel.DELETE} File: "{file_obj.filepath}" not found.')
            else:
                log.info(f'{ActionLabel.DELETE} Deleted file "{file_obj.filepath}" (CID: {cid})')
                remove(file_obj.filepath)
                parent = str(Path(file_obj.filepath).parent)
                if is_sentinel:
                    cleanup_empty_dir(parent, bucket_root=bucket_dir)
                else:
                    ensure_sentinel_in_dir(parent)
            del_sha256_hash(file_obj.filepath)
            # Causal tombstone (ADR-0003): inherit the stored version clock for
            # the path and tick it by the deleting peer; sentinel helpers drop
            # any stale marker instead (never exchanged, never tombstoned).
            if record_path_deletion(
                bucket,
                bucket_dir,
                file_obj.filepath,
                sha256sum=file_obj.sha256sum,
                cid=cid,
            ):
                # Drop the causal version entry so a re-created file starts fresh.
                clear_version(bucket, bucket_dir, file_obj.filepath)
            return Event(cid, bucket_dir, bucket, EventName.deleted, file_obj)

    return None


async def handle_renamed_event(
    src: dict[str, Any] | None,
    dst: dict[str, Any] | None,
    bucket_dir: str,
    bucket: str,
    cid: str,
) -> Event | None:
    """
    Handle 'renamed' events - rename/move files or directories.

    Args:
        src: Source object data from the event.
        dst: Destination object data from the event.
        bucket_dir: Full path to the bucket directory.
        bucket: Bucket name.
        cid: Client ID.

    Returns:
        Event object if successful, None otherwise.
    """
    if not src or not dst:
        return None

    if src.get("path"):
        src_dir = Dir.from_dict(src, bucket_dir)
        dst_dir = Dir.from_dict(dst, bucket_dir)
        if not _validate_path(src_dir.path, bucket_dir):
            return None
        if not _validate_path(dst_dir.path, bucket_dir):
            return None
        if src_dir.path and dst_dir.path:
            log.info(
                f'{ActionLabel.RENAME} Renamed "{src_dir.path}" -> "{dst_dir.path}" (CID: {cid})'
            )
            rename(src_dir.path, dst_dir.path)
            # Causal bookkeeping is best-effort: the on-disk rename already
            # happened, so a failure here must not abort the event broadcast
            # (peers would never learn of the rename).
            try:
                move_version_prefix(bucket, bucket_dir, src_dir.path, dst_dir.path)
                # The destination may carry deletion markers from an earlier
                # delete; the rename repopulates it, so they must be dropped or
                # peers could treat the renamed tree as deleted.
                clear_deletion_tree(bucket, bucket_dir, dst_dir.path)
            except Exception as e:  # pragma: no cover - best-effort bookkeeping
                log.warning(f"Could not update causal state for {dst_dir.path}: {e}")
            return Event(cid, bucket_dir, bucket, EventName.renamed, src_dir, dst_dir)

    elif src.get("filepath"):
        src_file = File.from_dict(src, bucket_dir, False)
        dst_file = File.from_dict(dst, bucket_dir, False)
        if not _validate_path(src_file.filepath, bucket_dir):
            return None
        if not _validate_path(dst_file.filepath, bucket_dir):
            return None
        if src_file.filepath and dst_file.filepath:
            log.info(
                f'{ActionLabel.RENAME} Renamed "{src_file.filepath}" -> "{dst_file.filepath}" (CID: {cid})'
            )
            rename(src_file.filepath, dst_file.filepath)
            # Causal bookkeeping is best-effort: the on-disk rename already
            # happened, so a failure here must not abort the event broadcast
            # (peers would never learn of the rename).
            try:
                move_version(bucket, bucket_dir, src_file.filepath, dst_file.filepath)
                # The destination may carry a stale deletion marker from an
                # earlier delete; creating content there (as in upload/add) must
                # drop it.
                clear_deletion(bucket, bucket_dir, dst_file.filepath)
            except Exception as e:  # pragma: no cover - best-effort bookkeeping
                log.warning(f"Could not update causal state for {dst_file.filepath}: {e}")
            return Event(cid, bucket_dir, bucket, EventName.renamed, src_file, dst_file)

    return None


async def handle_symlink_event(
    src: dict[str, Any] | None,
    bucket_dir: str,
    bucket: str,
    cid: str,
) -> Event | None:
    """
    Handle 'symlink' events - create or update symbolic links.

    Args:
        src: Source object data from the event.
        bucket_dir: Full path to the bucket directory.
        bucket: Bucket name.
        cid: Client ID.

    Returns:
        Event object if successful, None otherwise.
    """
    if not src:
        return None

    if src.get("type") == "symlink":
        link = Symlink.from_dict(src, strip_dir_prefix=False)
        link.filepath = f"{bucket_dir}/{link.filepath.lstrip('/')}"
        target = link.target

        if link.filepath and target:
            if not _validate_path(link.filepath, bucket_dir):
                return None

            if not is_safe_symlink_target(link.filepath, target, bucket_dir):
                log.warning(
                    f"{ActionLabel.SECURITY} Rejected unsafe symlink {link.filepath!r} -> {target!r} "
                    f"(target escapes bucket directory)"
                )
                return None

            log.info(f'{ActionLabel.SYMLINK} Symlink "{link.filepath}" -> "{target}" (CID: {cid})')
            parent_dir = str(Path(link.filepath).parent)
            if not Path(parent_dir).exists():
                Path(parent_dir).mkdir(parents=True, exist_ok=True)

            if Path(link.filepath).is_symlink() or Path(link.filepath).exists():
                unlink(link.filepath)

            os_symlink(target, link.filepath)
            return Event(cid, bucket_dir, bucket, EventName.symlink, link)

    return None
