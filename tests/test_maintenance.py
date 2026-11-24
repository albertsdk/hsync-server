# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Tests for the opt-in conflicted-copy maintenance sweep."""

from __future__ import annotations

import os
from pathlib import Path

from hsync_server import maintenance


def _touch(path: Path, mtime: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    os.utime(path, (mtime, mtime))


def _conflict(root: Path, bucket: str, stem: str) -> Path:
    return root / bucket / f"{stem}_conflicted_copy_20200101T000000Z_cid1.txt"


def test_prune_targets_only_old_regular_conflict_copies(tmp_path, monkeypatch):
    root = tmp_path / "buckets"
    now = 10_000_000.0
    old = now - 10 * 86400
    recent = now - 1 * 86400

    old_copy = _conflict(root, "photos", "a")
    new_copy = _conflict(root, "photos", "b")
    plain = root / "photos" / "c.txt"
    _touch(old_copy, old)
    _touch(new_copy, recent)
    _touch(plain, old)
    other = _conflict(root, "docs", "d")
    _touch(other, old)

    link = _conflict(root, "photos", "e")
    try:
        link.symlink_to(plain)
        has_link = True
    except OSError:  # symlink needs a privilege on Windows
        has_link = False

    calls: list[str] = []
    monkeypatch.setattr(
        maintenance,
        "delete_path_state",
        lambda b, p, *, bucket_dir, cid: calls.append(f"del:{p}"),
    )
    monkeypatch.setattr(
        maintenance,
        "cleanup_deleted_path",
        lambda p, *, bucket_dir: calls.append(f"clean:{p}"),
    )

    dry = maintenance.prune_conflicted_copies(
        buckets_root=root, older_than_s=7 * 86400, dry_run=True, now=now
    )
    assert set(dry) == {old_copy, other}
    assert calls == []  # dry run touches nothing

    removed = maintenance.prune_conflicted_copies(
        buckets_root=root, older_than_s=7 * 86400, now=now
    )
    assert set(removed) == {old_copy, other}
    assert sorted(calls) == sorted(
        [f"del:{other}", f"clean:{other}", f"del:{old_copy}", f"clean:{old_copy}"]
    )

    # Only the eligible files were selected; the recent copy, the ordinary file
    # and (when creatable) the symlink were never targeted.
    assert new_copy.exists() and plain.exists()
    if has_link:
        assert link.is_symlink()


def test_prune_can_be_restricted_to_one_bucket(tmp_path, monkeypatch):
    root = tmp_path / "buckets"
    moment = 1_000.0
    a = _conflict(root, "a", "x")
    b = _conflict(root, "b", "y")
    _touch(a, moment)
    _touch(b, moment)
    monkeypatch.setattr(maintenance, "delete_path_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(maintenance, "cleanup_deleted_path", lambda *args, **kwargs: None)

    assert maintenance.prune_conflicted_copies(
        buckets_root=root, bucket="a", older_than_s=0, now=moment + 1
    ) == [a]


# ── overwrite snapshots (`<path>.backup`) ───────────────────────────────
def test_backup_sweep_keeps_the_only_copy_of_a_crashed_publish(tmp_path):
    """A snapshot without its primary is the data the crash left: never removed."""
    root = tmp_path / "buckets"
    now = 10_000_000.0
    stale = now - 30 * 86400

    orphan = root / "photos" / "lost.txt.backup"
    _touch(orphan, stale)

    assert maintenance.prune_orphaned_backups(buckets_root=root, older_than_s=86400, now=now) == []
    assert orphan.exists()


def test_backup_sweep_removes_only_stale_snapshots_of_a_live_path(tmp_path):
    root = tmp_path / "buckets"
    now = 10_000_000.0
    stale = now - 30 * 86400
    recent = now - 60

    primary = root / "photos" / "photo.jpg"
    _touch(primary, stale)
    old_snapshot = root / "photos" / "photo.jpg.backup"
    _touch(old_snapshot, stale)
    fresh_snapshot = root / "docs" / "notes.txt.backup"
    _touch(fresh_snapshot, recent)
    _touch(root / "docs" / "notes.txt", stale)
    # An ordinary file keeps the conflicted-copy sweep's territory.
    _touch(root / "photos" / "other.jpg", stale)

    dry = maintenance.prune_orphaned_backups(
        buckets_root=root, older_than_s=7 * 86400, dry_run=True, now=now
    )
    assert dry == [old_snapshot]
    assert old_snapshot.exists(), "a dry run must not delete"

    removed = maintenance.prune_orphaned_backups(buckets_root=root, older_than_s=7 * 86400, now=now)
    assert removed == [old_snapshot]
    assert not old_snapshot.exists()
    # The live path, the recent snapshot and every other file are untouched.
    assert primary.exists() and fresh_snapshot.exists() and (root / "photos" / "other.jpg").exists()


def test_backup_sweep_can_be_restricted_to_one_bucket(tmp_path):
    root = tmp_path / "buckets"
    moment = 1_000.0
    for name in ("a", "b"):
        _touch(root / name / "keep.txt", moment)
        _touch(root / name / "keep.txt.backup", moment)

    assert maintenance.prune_orphaned_backups(
        buckets_root=root, bucket="a", older_than_s=0, now=moment + 1
    ) == [root / "a" / "keep.txt.backup"]
    assert (root / "b" / "keep.txt.backup").exists()
