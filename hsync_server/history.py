# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Server history store (ADR-0057).

The server keeps one blob per path, so an overwrite destroys the previous content
and a delete destroys the last one. This module preserves those bytes, per bucket,
under ``<state dir>/history`` — outside every served bucket, so no peer sees them
and the wire protocol is unchanged.

Capturing is **link-or-copy**: a hardlink costs no bytes when the store shares the
volume with the bucket, and the caller keeps owning the original afterwards (the
delete path unlinks it, the overwrite path replaces it). Every failure returns
``None`` after a log line instead of raising: a recovery copy must never abort the
operation it is trying to make recoverable.
"""

from __future__ import annotations

import json
import os
import shutil
import stat as stat_module
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from hsync_common.constants import HISTORY_DIRNAME, HISTORY_ENTRY_FILENAME, HISTORY_REASONS
from hsync_common.defaults import HISTORY_RETENTION_DAYS_DEFAULT
from hsync_common.models.file import File as LocalFile
from hsync_common.models.history import (
    HistoryEntry,
    history_bucket_dir,
    history_entry_dir,
    history_entry_payload_path,
    history_root,
    parse_history_entry,
)
from hsync_common.models.trash import is_safe_relpath
from loguru import logger as log

from hsync_server.config import get_state_dir

__all__ = [
    "HistoryItem",
    "PruneReport",
    "buckets",
    "capture_file",
    "entries",
    "entry",
    "prune",
    "storage_bytes",
]

_SECONDS_PER_DAY = 86_400


@dataclass(frozen=True)
class HistoryItem:
    """One stored entry: the minted id, its validated record and its payload."""

    entry_id: str
    entry: HistoryEntry
    payload: Path


@dataclass(frozen=True)
class PruneReport:
    """Outcome of a retention sweep: directories removed and bytes reclaimed."""

    removed: int
    freed: int


def _root() -> Path:
    """Return the history root of this installation."""
    return history_root(Path(get_state_dir()) / HISTORY_DIRNAME)


def _mint_entry_id() -> str:
    """Mint an id that sorts by age: ``<unix seconds>-<random hex>``."""
    return f"{int(time.time())}-{uuid.uuid4().hex[:12]}"


def _relpath(abs_path: str, bucket_dir: str) -> str | None:
    """Bucket-relative POSIX path of *abs_path*, or ``None`` when it escapes."""
    try:
        return Path(abs_path).relative_to(bucket_dir).as_posix()
    except ValueError:
        return None


def _write_entry(directory: Path, record: HistoryEntry) -> bool:
    """Write ``entry.json`` atomically; a partial record is never observable."""
    target = directory / HISTORY_ENTRY_FILENAME
    partial = directory / (HISTORY_ENTRY_FILENAME + ".part")
    try:
        partial.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
        os.chmod(partial, 0o640)
        os.replace(partial, target)
    except OSError:
        log.warning(f"history: could not write {target}")
        partial.unlink(missing_ok=True)
        return False
    return True


def capture_file(
    bucket: str,
    abs_path: str,
    *,
    bucket_dir: str,
    reason: str,
    cid: str | None = None,
) -> str | None:
    """Preserve *abs_path* in the bucket's history; return the entry id or ``None``.

    Symlinks and special files are skipped (their bytes are not the ones a restore
    would need), and so is anything outside *bucket_dir*.
    """
    if reason not in HISTORY_REASONS:
        raise ValueError(f"unknown history reason: {reason!r}")
    relpath = _relpath(abs_path, bucket_dir)
    if relpath is None or not is_safe_relpath(relpath):
        return None
    src = Path(abs_path)
    try:
        info = src.lstat()
    except OSError:
        return None
    if not stat_module.S_ISREG(info.st_mode):
        return None
    src_file = LocalFile.get(abs_path)
    if src_file is None:
        return None
    entry_id = _mint_entry_id()
    directory: Path | None = None
    try:
        directory = history_entry_dir(_root(), bucket, entry_id)
        payload = history_entry_payload_path(directory, relpath)
        payload.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(src, payload)
        except OSError:
            # Another volume (or a filesystem without links): copy the bytes.
            partial = payload.with_name(payload.name + ".part")
            shutil.copy2(src, partial)
            os.replace(partial, payload)
    except (OSError, ValueError):
        # Without its record the directory is unreadable garbage; leaving it
        # would inflate the store until a prune sweep found it.
        if directory is not None:
            shutil.rmtree(directory, ignore_errors=True)
        log.warning(f'history: could not preserve "{abs_path}"')
        return None
    record = HistoryEntry(
        relpath=relpath,
        sha256=src_file.sha256sum,
        size=int(info.st_size),
        mtime_ns=int(info.st_mtime_ns),
        mode=int(info.st_mode) & 0o777,
        recorded_at=int(time.time()),
        reason=reason,
        cid=cid,
        bucket=bucket,
    )
    if not _write_entry(directory, record):
        shutil.rmtree(directory, ignore_errors=True)
        return None
    return entry_id


def _directory_size(directory: Path) -> int:
    """Bytes under *directory*, used only for a record that cannot be parsed."""
    total = 0
    for file in directory.rglob("*"):
        try:
            if file.is_file():
                total += file.stat().st_size
        except OSError:
            continue
    return total


def buckets() -> list[str]:
    """Return the buckets that have preserved entries."""
    root = _root()
    if not root.is_dir():
        return []
    return sorted(child.name for child in root.iterdir() if child.is_dir())


def entry(bucket: str, entry_id: str) -> HistoryItem | None:
    """Return one entry by id, or ``None`` when it is absent or unreadable."""
    root = _root()
    try:
        directory = history_entry_dir(root, bucket, entry_id)
        raw = json.loads((directory / HISTORY_ENTRY_FILENAME).read_text(encoding="utf-8"))
        parsed = parse_history_entry(raw)
        if parsed is None:
            return None
        payload = history_entry_payload_path(directory, parsed["relpath"])
    except (OSError, ValueError):
        return None
    return HistoryItem(entry_id=entry_id, entry=parsed, payload=payload)


def entries(bucket: str) -> list[HistoryItem]:
    """Return the bucket's entries, newest first; a damaged record is skipped."""
    root = _root()
    try:
        directory = history_bucket_dir(root, bucket)
    except ValueError:
        return []
    if not directory.is_dir():
        return []
    items = [
        found
        for child in directory.iterdir()
        if child.is_dir() and (found := entry(bucket, child.name)) is not None
    ]
    items.sort(key=lambda item: (item.entry["recorded_at"], item.entry_id), reverse=True)
    return items


def storage_bytes(bucket: str | None = None) -> int:
    """Bytes of preserved payloads, for one bucket or for every bucket."""
    names = [bucket] if bucket is not None else buckets()
    return sum(item.entry["size"] for name in names for item in entries(name))


def prune(
    *,
    bucket: str | None = None,
    retention_days: int = HISTORY_RETENTION_DAYS_DEFAULT,
    max_bytes: int | None = None,
    dry_run: bool = False,
    now: float | None = None,
) -> PruneReport:
    """Drop entries older than *retention_days*, then the oldest over *max_bytes*.

    Age is the primary bound; the byte budget only reacts to a burst (a hot file
    rewritten every minute fits the window but not the disk). ``max_bytes=None``
    keeps the age bound alone. ``dry_run`` reports the same counts without
    removing anything.
    """
    moment = time.time() if now is None else now
    cutoff = moment - max(retention_days, 0) * _SECONDS_PER_DAY
    root = _root()
    if bucket is not None:
        try:
            directories = [history_bucket_dir(root, bucket)]
        except ValueError:
            return PruneReport(removed=0, freed=0)
    else:
        directories = [p for p in root.iterdir() if p.is_dir()] if root.is_dir() else []

    kept: list[tuple[int, int, Path]] = []
    removed = 0
    freed = 0
    for directory in directories:
        if not directory.is_dir():
            continue
        for child in directory.iterdir():
            if not child.is_dir():
                continue
            try:
                raw = json.loads((child / HISTORY_ENTRY_FILENAME).read_text(encoding="utf-8"))
                parsed = parse_history_entry(raw)
            except (OSError, ValueError):
                parsed = None
            if parsed is None:
                # An unreadable entry cannot be restored; keep it out of the way
                # and let the budget reclaim it first.
                recorded_at = 0
                size = _directory_size(child)
            else:
                recorded_at = parsed["recorded_at"]
                size = parsed["size"]
            if recorded_at and recorded_at < cutoff:
                if not dry_run:
                    shutil.rmtree(child, ignore_errors=True)
                removed += 1
                freed += size
                continue
            kept.append((recorded_at, size, child))

    if max_bytes is not None and max_bytes >= 0:
        kept.sort(key=lambda item: (item[0], item[2].name))
        total = sum(size for _when, size, _child in kept)
        while kept and total > max_bytes:
            _when, size, child = kept.pop(0)
            if not dry_run:
                shutil.rmtree(child, ignore_errors=True)
            removed += 1
            freed += size
            total -= size
    return PruneReport(removed=removed, freed=freed)
