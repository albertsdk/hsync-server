# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Black-box tests for ``hsync-server admin maintenance prune-conflicts``.

Runs the real command as a subprocess against an isolated buckets directory and
observes only its exit code, its stdout and the files on disk. The command is
opt-in: a ``--dry-run`` must report the targets without touching them, and a real
run must reclaim only the canonical, old conflicted copies.

Requires ``HSYNC_BB_SERVER=1`` to run.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

_BB_SERVER_ENABLED = os.environ.get("HSYNC_BB_SERVER") == "1"
_BB_SERVER_REASON = "Black-box server-mode tests require HSYNC_BB_SERVER=1"

pytestmark = pytest.mark.skipif(not _BB_SERVER_ENABLED, reason=_BB_SERVER_REASON)

_ADMIN_SECRET = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
_COMMAND = ("admin", "maintenance", "prune-conflicts")


def _run(*args: str, buckets: Path, state: Path) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("HSYNC_")}
    env.update(
        {
            "HSYNC_ADMIN_KEY": _ADMIN_SECRET,
            "HSYNC_BUCKETS": str(buckets),
            "HSYNC_STATE_DIR": str(state),
            "HSYNC_SCRYPT_N": "16",
            "HSYNC_SCRYPT_MAXMEM": "33554432",
            "HSYNC_MIN_FREE_DISK_SPACE": "0",
        }
    )
    return subprocess.run(
        [sys.executable, "-m", "hsync_server", *_COMMAND, *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )


def _touch(path: Path, *, age_seconds: float) -> None:
    path.write_bytes(b"conflict payload\n")
    stamp = time.time() - age_seconds
    os.utime(path, (stamp, stamp))


class TestPruneConflictsCommand:
    def test_dry_run_reports_then_real_run_reclaims_only_old_copies(self, tmp_path, bb_sample):
        # Given one bucket with an old canonical conflict copy, a recent one, an
        # ordinary file and a non-canonical name that merely contains the marker
        buckets = tmp_path / "buckets"
        bucket = buckets / "bb-prune"
        bucket.mkdir(parents=True)
        ten_days = 10 * 86400
        old_copy = (
            bucket
            / bb_sample(prefix="old", suffix="_conflicted_copy_20200101T000000Z_cid1.txt").name
        )
        recent_copy = bucket / (
            bb_sample(prefix="recent", suffix="_conflicted_copy_20200102T000000Z_cid1.txt").name
        )
        ordinary = bucket / bb_sample(prefix="ordinary").name
        lookalike = bucket / "notes_conflicted_copy_backup.txt"
        _touch(old_copy, age_seconds=ten_days)
        _touch(recent_copy, age_seconds=86400)
        _touch(ordinary, age_seconds=ten_days)
        _touch(lookalike, age_seconds=ten_days)
        state = tmp_path / "state"

        # When a dry run targets copies older than seven days
        dry = _run(
            "--bucket",
            "bb-prune",
            "--older-than-days",
            "7",
            "--dry-run",
            buckets=buckets,
            state=state,
        )

        # Then it reports the old copy and deletes nothing
        assert dry.returncode == 0, dry.stderr
        assert str(old_copy) in dry.stdout
        assert (
            old_copy.exists() and recent_copy.exists() and ordinary.exists() and lookalike.exists()
        )

        # When the real run executes the same policy
        run = _run("--bucket", "bb-prune", "--older-than-days", "7", buckets=buckets, state=state)

        # Then only the old canonical conflict copy is gone
        assert run.returncode == 0, run.stderr
        assert not old_copy.exists(), "the old conflicted copy was not reclaimed"
        assert recent_copy.exists(), "a recent conflicted copy must be kept"
        assert ordinary.exists(), "an ordinary file must never be touched"
        assert lookalike.exists(), "only the canonical marker is eligible"

    def test_missing_bucket_is_reported_without_deleting(self, tmp_path):
        buckets = tmp_path / "buckets"
        buckets.mkdir()
        result = _run(
            "--bucket",
            "does-not-exist",
            "--older-than-days",
            "1",
            buckets=buckets,
            state=tmp_path / "state",
        )
        assert result.returncode != 0
        assert "does not exist" in (result.stdout + result.stderr)
