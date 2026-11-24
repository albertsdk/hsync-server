# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""The server history store (ADR-0057): capture, listing and retention.

The store must never be able to break the operation it is trying to make
recoverable, so the tests pin both sides: what it preserves (bytes, metadata, a
valid entry) and what it refuses (a path outside the bucket, a symlink, an
unknown reason).
"""

from __future__ import annotations

import json
import os
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
from hsync_server import maintenance

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
