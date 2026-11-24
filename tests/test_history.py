# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""The server history store (ADR-0057): capture, listing and retention.

The store must never be able to break the operation it is trying to make
recoverable, so the tests pin both sides: what it preserves (bytes, metadata, a
valid entry) and what it refuses (a path outside the bucket, a symlink, an
unknown reason).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest
from hsync_common.constants import (
    HISTORY_ENTRY_FILENAME,
    HISTORY_PAYLOAD_DIRNAME,
    HISTORY_REASON_DELETE,
    HISTORY_REASON_OVERWRITE,
)
from hsync_common.models.history import history_root, parse_history_entry
from typer.testing import CliRunner

from hsync_server import __main__ as cli
from hsync_server import config as server_config
from hsync_server import history as history_mod
from hsync_server import maintenance, tombstones, versions

runner = CliRunner()


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    """Point the store at a temporary state dir (the real resolution path)."""
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(history_mod, "get_state_dir", lambda: str(state))
    # The admin CLI imports the resolver inside each command body.
    monkeypatch.setattr(server_config, "get_state_dir", lambda: str(state))
    return state


@pytest.fixture
def bucket_dir(tmp_path):
    root = tmp_path / "buckets" / "photos"
    root.mkdir(parents=True)
    return root


def _write(path: Path, payload: bytes = b"content") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def _replace(path: Path, payload: bytes) -> Path:
    """Replace *path* the way a publish does: a new inode, never an in-place write.

    The store captures by hardlink, so truncating the original in place would also
    rewrite the preserved copy; every writer in the server publishes through an
    atomic rename instead.
    """
    staged = path.with_name(path.name + ".staged")
    staged.write_bytes(payload)
    os.replace(staged, path)
    return path


def _capture(
    bucket_dir: Path,
    relpath: str,
    *,
    payload: bytes = b"content",
    reason: str = HISTORY_REASON_OVERWRITE,
) -> str | None:
    """Capture *relpath* of the ``photos`` bucket, as both hooks do."""
    return history_mod.capture_file(
        "photos",
        str(_write(bucket_dir / relpath, payload)),
        bucket_dir=str(bucket_dir),
        reason=reason,
    )


def _entries(state: Path, bucket: str = "photos") -> list[Path]:
    root = state / "history" / bucket
    if not root.is_dir():
        return []
    return sorted(item for item in root.iterdir() if item.is_dir())


def test_the_store_lives_under_the_state_dir(state_dir) -> None:
    assert history_root(state_dir / "history").parent == state_dir
    assert _entries(state_dir) == []


def test_capture_preserves_the_bytes_and_a_valid_record(state_dir, bucket_dir) -> None:
    source = _write(bucket_dir / "docs" / "report.txt", b"the old bytes")

    entry_id = history_mod.capture_file(
        "photos",
        str(source),
        bucket_dir=str(bucket_dir),
        reason=HISTORY_REASON_OVERWRITE,
        cid="abcdefghijklmnopqrstu",
    )

    assert entry_id is not None
    entry = state_dir / "history" / "photos" / entry_id
    payload = entry / HISTORY_PAYLOAD_DIRNAME / "docs" / "report.txt"
    assert payload.read_bytes() == b"the old bytes"
    record = parse_history_entry(json.loads((entry / HISTORY_ENTRY_FILENAME).read_text()))
    assert record is not None
    assert record["relpath"] == "docs/report.txt"
    assert record["reason"] == HISTORY_REASON_OVERWRITE
    assert record["size"] == len(b"the old bytes")
    assert record["bucket"] == "photos"
    assert record["cid"] == "abcdefghijklmnopqrstu"
    # The original is still there: the caller decides what to do with it.
    assert source.exists()


def test_a_captured_file_survives_its_own_deletion(state_dir, bucket_dir) -> None:
    """The whole point: the bytes outlive the unlink that follows the capture."""
    source = _write(bucket_dir / "keep.txt", b"payload")

    entry_id = history_mod.capture_file(
        "photos",
        str(source),
        bucket_dir=str(bucket_dir),
        reason=HISTORY_REASON_DELETE,
        cid=None,
    )
    source.unlink()

    assert entry_id is not None
    stored = state_dir / "history" / "photos" / entry_id / HISTORY_PAYLOAD_DIRNAME / "keep.txt"
    assert stored.read_bytes() == b"payload"


def test_a_path_outside_the_bucket_is_refused(state_dir, bucket_dir, tmp_path) -> None:
    stranger = _write(tmp_path / "elsewhere.txt")

    assert (
        history_mod.capture_file(
            "photos",
            str(stranger),
            bucket_dir=str(bucket_dir),
            reason=HISTORY_REASON_DELETE,
        )
        is None
    )
    assert _entries(state_dir) == []


def test_a_symlink_is_not_preserved(state_dir, bucket_dir, tmp_path) -> None:
    target = _write(tmp_path / "target.txt")
    link = bucket_dir / "link.txt"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks need a privilege on this platform")

    assert (
        history_mod.capture_file(
            "photos",
            str(link),
            bucket_dir=str(bucket_dir),
            reason=HISTORY_REASON_DELETE,
        )
        is None
    )


def test_an_unknown_reason_is_a_programming_error(state_dir, bucket_dir) -> None:
    source = _write(bucket_dir / "x.txt")

    with pytest.raises(ValueError):
        history_mod.capture_file("photos", str(source), bucket_dir=str(bucket_dir), reason="sync")


def test_entries_are_listed_newest_first(state_dir, bucket_dir) -> None:
    for name in ("a.txt", "b.txt"):
        history_mod.capture_file(
            "photos",
            str(_write(bucket_dir / name)),
            bucket_dir=str(bucket_dir),
            reason=HISTORY_REASON_OVERWRITE,
        )

    items = history_mod.entries("photos")

    assert {item.entry["relpath"] for item in items} == {"a.txt", "b.txt"}
    # Newest first: a monotonic (non-increasing) record time.
    times = [item.entry["recorded_at"] for item in items]
    assert times == sorted(times, reverse=True)


def test_a_damaged_record_is_skipped(state_dir, bucket_dir) -> None:
    history_mod.capture_file(
        "photos",
        str(_write(bucket_dir / "ok.txt")),
        bucket_dir=str(bucket_dir),
        reason=HISTORY_REASON_DELETE,
    )
    broken = state_dir / "history" / "photos" / "1700000000-deadbeef"
    broken.mkdir(parents=True)
    (broken / HISTORY_ENTRY_FILENAME).write_text("{not json", encoding="utf-8")

    assert [item.entry["relpath"] for item in history_mod.entries("photos")] == ["ok.txt"]


def test_prune_drops_entries_past_the_window(state_dir, bucket_dir) -> None:
    entry_id = history_mod.capture_file(
        "photos",
        str(_write(bucket_dir / "old.txt")),
        bucket_dir=str(bucket_dir),
        reason=HISTORY_REASON_DELETE,
    )
    assert entry_id is not None

    future = history_mod.time.time() + 8 * 86400
    report = history_mod.prune(retention_days=7, now=future)

    assert report.removed == 1 and report.freed > 0
    assert _entries(state_dir) == []


def test_prune_keeps_an_entry_inside_the_window(state_dir, bucket_dir) -> None:
    history_mod.capture_file(
        "photos",
        str(_write(bucket_dir / "fresh.txt")),
        bucket_dir=str(bucket_dir),
        reason=HISTORY_REASON_DELETE,
    )

    report = history_mod.prune(retention_days=7, now=history_mod.time.time() + 60)

    assert report.removed == 0
    assert len(_entries(state_dir)) == 1


def test_the_byte_budget_drops_the_oldest_first(state_dir, bucket_dir) -> None:
    for name in ("a.txt", "b.txt"):
        history_mod.capture_file(
            "photos",
            str(_write(bucket_dir / name, b"0123456789")),
            bucket_dir=str(bucket_dir),
            reason=HISTORY_REASON_OVERWRITE,
        )

    # Two entries of 10 payload bytes: a 15-byte budget reclaims exactly one.
    report = history_mod.prune(retention_days=7, max_bytes=15)

    assert report.removed == 1
    assert report.freed == 10
    assert len(history_mod.entries("photos")) == 1


def test_the_delete_path_captures_before_the_unlink(state_dir, bucket_dir, monkeypatch) -> None:
    """``delete_path_state`` keeps the bytes on its way to the tombstone."""
    source = _write(bucket_dir / "gone.txt", b"last copy")
    monkeypatch.setattr(maintenance, "record_path_deletion", lambda *a, **k: None)
    monkeypatch.setattr(maintenance, "clear_version", lambda *a, **k: None)

    maintenance.delete_path_state(
        "photos", str(source), bucket_dir=str(bucket_dir), cid="abcdefghijklmnopqrstu"
    )

    assert not source.exists()
    items = history_mod.entries("photos")
    assert [item.entry["relpath"] for item in items] == ["gone.txt"]
    stored = (
        state_dir / "history" / "photos" / items[0].entry_id / HISTORY_PAYLOAD_DIRNAME / "gone.txt"
    )
    assert stored.read_bytes() == b"last copy"


class TestRestore:
    """ADR-0057 restore: the entry goes back to a live, causally newer file."""

    def test_the_bytes_and_the_recorded_metadata_come_back(self, state_dir, bucket_dir):
        source = _write(bucket_dir / "docs" / "report.txt", b"first draft")
        os.utime(source, ns=(1_600_000_000_000_000_000, 1_600_000_000_000_000_000))
        maintenance.delete_path_state("photos", str(source), bucket_dir=str(bucket_dir), cid=None)
        (item,) = history_mod.entries("photos")
        assert not source.exists()

        outcome = maintenance.restore_entry("photos", item.entry_id, bucket_dir=str(bucket_dir))

        assert outcome.status is maintenance.RestoreStatus.RESTORED
        assert outcome.relpath == "docs/report.txt"
        assert outcome.size == len(b"first draft")
        assert source.read_bytes() == b"first draft"
        assert source.stat().st_mtime == 1_600_000_000

    def test_a_restored_path_is_no_longer_deleted(self, state_dir, bucket_dir):
        """The invariant: the next cycle must not answer with the old deletion."""
        source = _write(bucket_dir / "gone.txt", b"payload")
        maintenance.delete_path_state(
            "photos",
            str(source),
            bucket_dir=str(bucket_dir),
            cid="abcdefghijklmnopqrstu",
        )
        (item,) = history_mod.entries("photos")

        maintenance.restore_entry("photos", item.entry_id, bucket_dir=str(bucket_dir))

        assert tombstones.deletion_vclock("photos", str(bucket_dir), str(source)) is None
        recorded = versions.server_version_info("photos", str(bucket_dir), str(source))
        assert recorded is not None
        server_cid = versions.get_server_cid()
        assert recorded["origin_cid"] == server_cid
        # The restore supersedes the deletion instead of racing it: every clock
        # the marker carried survives in the new version.
        assert recorded["vclock"][server_cid] >= 1
        assert recorded["vclock"]["abcdefghijklmnopqrstu"] >= 1

    def test_the_entry_survives_the_restore(self, state_dir, bucket_dir):
        """Retention, not the restore, decides when the copy goes away."""
        source = _write(bucket_dir / "keep.txt", b"payload")
        maintenance.delete_path_state("photos", str(source), bucket_dir=str(bucket_dir), cid=None)
        (item,) = history_mod.entries("photos")

        maintenance.restore_entry("photos", item.entry_id, bucket_dir=str(bucket_dir))

        assert [found.entry_id for found in history_mod.entries("photos")] == [item.entry_id]

    def test_an_occupied_path_is_left_alone(self, state_dir, bucket_dir):
        entry_id = _capture(bucket_dir, "a.txt", payload=b"preserved")
        _replace(bucket_dir / "a.txt", b"current")

        outcome = maintenance.restore_entry("photos", entry_id, bucket_dir=str(bucket_dir))

        assert outcome.status is maintenance.RestoreStatus.CONFLICT
        assert (bucket_dir / "a.txt").read_bytes() == b"current"

    def test_overwriting_preserves_what_is_being_replaced(self, state_dir, bucket_dir):
        entry_id = _capture(bucket_dir, "a.txt", payload=b"preserved")
        _replace(bucket_dir / "a.txt", b"current")

        outcome = maintenance.restore_entry(
            "photos", entry_id, bucket_dir=str(bucket_dir), overwrite=True
        )

        assert outcome.status is maintenance.RestoreStatus.RESTORED
        assert (bucket_dir / "a.txt").read_bytes() == b"preserved"
        # Both captures share a second, so the order between them is arbitrary:
        # assert the set, not which entry got listed first.
        replaced = {
            (found.entry["sha256"], found.entry["reason"])
            for found in history_mod.entries("photos")
        }
        assert replaced == {
            (hashlib.sha256(b"preserved").hexdigest(), HISTORY_REASON_OVERWRITE),
            (hashlib.sha256(b"current").hexdigest(), HISTORY_REASON_OVERWRITE),
        }

    def test_restoring_the_same_bytes_twice_changes_nothing(self, state_dir, bucket_dir):
        entry_id = _capture(bucket_dir, "same.txt", payload=b"same")
        (bucket_dir / "same.txt").unlink()

        first = maintenance.restore_entry("photos", entry_id, bucket_dir=str(bucket_dir))
        second = maintenance.restore_entry("photos", entry_id, bucket_dir=str(bucket_dir))

        assert first.status is maintenance.RestoreStatus.RESTORED
        assert second.status is maintenance.RestoreStatus.UNCHANGED
        assert len(history_mod.entries("photos")) == 1  # no spurious capture

    def test_a_symlink_at_the_destination_is_not_replaced(self, state_dir, bucket_dir, tmp_path):
        entry_id = _capture(bucket_dir, "target.txt", payload=b"preserved")
        (bucket_dir / "target.txt").unlink()
        link = bucket_dir / "target.txt"
        try:
            link.symlink_to(_write(tmp_path / "elsewhere.txt", b"other"))
        except OSError:
            pytest.skip("symlinks need a privilege on this platform")

        outcome = maintenance.restore_entry(
            "photos", entry_id, bucket_dir=str(bucket_dir), overwrite=True
        )

        assert outcome.status is maintenance.RestoreStatus.CONFLICT
        assert link.is_symlink()

    def test_a_missing_payload_is_reported(self, state_dir, bucket_dir):
        entry_id = _capture(bucket_dir, "a.txt", payload=b"gone")
        (bucket_dir / "a.txt").unlink()
        history_mod.entry("photos", entry_id).payload.unlink()

        outcome = maintenance.restore_entry("photos", entry_id, bucket_dir=str(bucket_dir))

        assert outcome.status is maintenance.RestoreStatus.MISSING
        assert not (bucket_dir / "a.txt").exists()

    def test_a_damaged_payload_is_never_published(self, state_dir, bucket_dir):
        entry_id = _capture(bucket_dir, "a.txt", payload=b"0123456789")
        (bucket_dir / "a.txt").unlink()
        history_mod.entry("photos", entry_id).payload.write_bytes(b"tampered!")

        outcome = maintenance.restore_entry("photos", entry_id, bucket_dir=str(bucket_dir))

        assert outcome.status is maintenance.RestoreStatus.DAMAGED
        assert not (bucket_dir / "a.txt").exists()

    def test_a_refused_write_is_reported_not_raised(self, state_dir, bucket_dir):
        entry_id = _capture(bucket_dir, "docs/report.txt", payload=b"payload")
        shutil.rmtree(bucket_dir / "docs")
        _write(bucket_dir / "docs", b"a file where a directory must go")

        outcome = maintenance.restore_entry("photos", entry_id, bucket_dir=str(bucket_dir))

        assert outcome.status is maintenance.RestoreStatus.FAILED

    def test_an_unknown_entry_is_reported(self, state_dir, bucket_dir):
        outcome = maintenance.restore_entry(
            "photos", "1700000000-deadbeef", bucket_dir=str(bucket_dir)
        )

        assert outcome.status is maintenance.RestoreStatus.MISSING


class TestHistoryCommands:
    def test_list_prints_what_was_preserved(self, state_dir, bucket_dir):
        _capture(bucket_dir, "docs/report.txt")

        result = runner.invoke(cli.app, ["admin", "history", "list", "--bucket", "photos"])

        assert result.exit_code == 0, result.output
        assert "docs/report.txt" in result.output
        assert "overwrite" in result.output
        assert "1 entry" in result.output

    def test_list_emits_json(self, state_dir, bucket_dir):
        _capture(bucket_dir, "docs/report.txt", payload=b"abc")

        result = runner.invoke(
            cli.app, ["admin", "history", "list", "--bucket", "photos", "--json"]
        )

        assert result.exit_code == 0, result.output
        (entry,) = json.loads(result.output)
        assert entry["path"] == "docs/report.txt"
        assert entry["bucket"] == "photos"
        assert entry["reason"] == HISTORY_REASON_OVERWRITE
        assert entry["size"] == 3

    def test_list_covers_every_bucket_without_one_asked(self, state_dir, bucket_dir):
        _capture(bucket_dir, "a.txt")

        result = runner.invoke(cli.app, ["admin", "history", "list"])

        assert result.exit_code == 0, result.output
        assert "photos" in result.output

    def test_an_empty_store_says_so(self, state_dir):
        result = runner.invoke(cli.app, ["admin", "history", "list"])

        assert result.exit_code == 0, result.output
        assert "No preserved versions." in result.output

    def test_prune_dry_run_keeps_the_entries(self, state_dir, bucket_dir):
        _capture(bucket_dir, "a.txt")

        result = runner.invoke(
            cli.app,
            ["admin", "history", "prune", "--older-than-days", "0", "--max-size", "0", "--dry-run"],
        )

        assert result.exit_code == 0, result.output
        assert "Would remove 1 preserved version(s)" in result.output
        assert len(history_mod.entries("photos")) == 1

    def test_prune_removes_the_expired_entries(self, state_dir, bucket_dir):
        _capture(bucket_dir, "a.txt")

        result = runner.invoke(
            cli.app,
            ["admin", "history", "prune", "--older-than-days", "0", "--max-size", "0"],
        )

        assert result.exit_code == 0, result.output
        assert "Removed 1 preserved version(s)" in result.output
        assert history_mod.entries("photos") == []

    def test_an_invalid_bucket_is_refused(self, state_dir):
        result = runner.invoke(cli.app, ["admin", "history", "list", "--bucket", "../etc"])

        assert result.exit_code == 1
        assert "invalid bucket name" in result.output

    def test_restore_puts_the_file_back(self, state_dir, bucket_dir, monkeypatch):
        monkeypatch.setattr(server_config, "get_buckets_path", lambda: str(bucket_dir.parent))
        entry_id = _capture(bucket_dir, "a.txt", payload=b"abc", reason=HISTORY_REASON_DELETE)
        (bucket_dir / "a.txt").unlink()

        result = runner.invoke(
            cli.app, ["admin", "history", "restore", entry_id, "--bucket", "photos"]
        )

        assert result.exit_code == 0, result.output
        assert "Restored a.txt (3 B)." in result.output
        assert (bucket_dir / "a.txt").read_bytes() == b"abc"

    def test_restore_emits_json(self, state_dir, bucket_dir, monkeypatch):
        monkeypatch.setattr(server_config, "get_buckets_path", lambda: str(bucket_dir.parent))
        entry_id = _capture(bucket_dir, "a.txt", payload=b"abc", reason=HISTORY_REASON_DELETE)
        (bucket_dir / "a.txt").unlink()

        result = runner.invoke(
            cli.app,
            ["admin", "history", "restore", entry_id, "--bucket", "photos", "--json"],
        )

        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == {
            "status": "restored",
            "path": "a.txt",
            "file": str(bucket_dir / "a.txt"),
            "size": 3,
        }

    def test_restore_asks_before_replacing_a_file(self, state_dir, bucket_dir, monkeypatch):
        monkeypatch.setattr(server_config, "get_buckets_path", lambda: str(bucket_dir.parent))
        entry_id = _capture(bucket_dir, "a.txt", payload=b"preserved")
        _replace(bucket_dir / "a.txt", b"current")
        command = ["admin", "history", "restore", entry_id, "--bucket", "photos"]

        refused = runner.invoke(cli.app, command)
        assert refused.exit_code == 1
        assert "pass --overwrite" in refused.output
        assert (bucket_dir / "a.txt").read_bytes() == b"current"

        replaced = runner.invoke(cli.app, [*command, "--overwrite"])
        assert replaced.exit_code == 0, replaced.output
        assert (bucket_dir / "a.txt").read_bytes() == b"preserved"

    def test_restore_reports_an_unknown_entry(self, state_dir, bucket_dir, monkeypatch):
        monkeypatch.setattr(server_config, "get_buckets_path", lambda: str(bucket_dir.parent))

        result = runner.invoke(
            cli.app,
            ["admin", "history", "restore", "1700000000-deadbeef", "--bucket", "photos"],
        )

        assert result.exit_code == 1
        assert "no usable history entry" in result.output

    def test_restore_refuses_an_invalid_bucket(self, state_dir):
        result = runner.invoke(
            cli.app,
            ["admin", "history", "restore", "1700000000-deadbeef", "--bucket", "../etc"],
        )

        assert result.exit_code == 1
        assert "invalid bucket name" in result.output


def test_the_entry_record_is_published_without_a_staging_file(state_dir, bucket_dir) -> None:
    history_mod.capture_file(
        "photos",
        str(_write(bucket_dir / "atomic.txt")),
        bucket_dir=str(bucket_dir),
        reason=HISTORY_REASON_DELETE,
    )

    (entry,) = _entries(state_dir)
    assert not (entry / (HISTORY_ENTRY_FILENAME + ".part")).exists()
    assert oct(os.stat(entry / HISTORY_ENTRY_FILENAME).st_mode & 0o777) == "0o640"
