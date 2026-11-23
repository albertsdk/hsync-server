# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Opt-in storage maintenance shared by the REST delete path and the CLI.

The delete endpoint and ``hsync-server admin maintenance prune-conflicts`` must
remove a path the same way: write the causal tombstone, unlink, drop the hash
and version state, then clean the emptied directory. One implementation means a
maintenance sweep can never desync the state the sync protocol relies on. The
caller passes the bucket directory it already resolved, so this module never
re-derives it.
"""

from __future__ import annotations

import os
import stat as stat_module
import time
from pathlib import Path

from hsync_common.models.arbitration import is_conflicted_copy
from hsync_common.models.file import File as LocalFile
from hsync_common.models.file import invalidate_sha256_cache
from loguru import logger as log

from hsync_server.api.file_utils import prune_empty_dirs
from hsync_server.constants import BACKUP_SUFFIX, ActionLabel
from hsync_server.tombstones import record_path_deletion
from hsync_server.versions import clear_version


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


def cleanup_deleted_path(abs_path: str, *, bucket_dir: str) -> None:
    """Drop the ``.backup`` snapshot and prune the directory the delete vacated."""
    backup_file = f"{abs_path}{BACKUP_SUFFIX}"
    if Path(backup_file).exists():
        os.remove(backup_file)
        log.info(f'{ActionLabel.DELETE} Backup file: "{backup_file}" deleted successfully')
    prune_empty_dirs(str(Path(abs_path).parent), bucket_root=bucket_dir)


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
    names = (
        [bucket]
        if bucket is not None
        else sorted(p.name for p in buckets_root.iterdir() if p.is_dir())
    )
    removed: list[Path] = []
    for name in names:
        root = buckets_root / name
        if not root.is_dir():
            continue
        bucket_dir = str(root)
        for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
            for filename in filenames:
                if not is_conflicted_copy(filename):
                    continue
                path = Path(dirpath) / filename
                try:
                    st = path.lstat()
                except OSError:
                    continue
                if not stat_module.S_ISREG(st.st_mode) or st.st_mtime > cutoff:
                    continue
                removed.append(path)
                if dry_run:
                    continue
                delete_path_state(name, str(path), bucket_dir=bucket_dir, cid=None)
                cleanup_deleted_path(str(path), bucket_dir=bucket_dir)
    return removed
