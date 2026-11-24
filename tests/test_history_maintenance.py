# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Retention for the server history store (ADR-0057).

The store exists so an operator can recover a replaced or deleted blob, which
means it must also shrink again on its own: without a sweep a deployment that
never runs a command would keep every byte it ever replaced. These tests pin the
policy (the shared defaults and the volume-derived budget) and the background
sweep that applies it, including its two failure modes — a broken store and a
shutdown request.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from hsync_common.constants import HISTORY_REASON_OVERWRITE
from hsync_common.defaults import (
    HISTORY_QUOTA_FLOOR_MB_DEFAULT,
    HISTORY_QUOTA_PERCENT_DEFAULT,
    HISTORY_RETENTION_DAYS_DEFAULT,
)
from hsync_common.disk import share_of_volume_bytes

from hsync_server import history as history_mod
from hsync_server import history_maintenance
from tests.history_helpers import age_entry


@pytest.fixture
def state_dir(tmp_path, monkeypatch) -> Path:
    """Point the store and the policy at a temporary state dir."""
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(history_mod, "get_state_dir", lambda: str(state))
    monkeypatch.setattr(history_maintenance, "get_state_dir", lambda: str(state))
    return state


@pytest.fixture
def bucket_dir(tmp_path) -> Path:
    root = tmp_path / "buckets" / "photos"
    root.mkdir(parents=True)
    return root


def _capture(bucket_dir: Path, relpath: str = "a.txt", payload: bytes = b"content") -> str:
    """Preserve *relpath* and return its entry id."""
    source = bucket_dir / relpath
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(payload)
    entry_id = history_mod.capture_file(
        "photos",
        str(source),
        bucket_dir=str(bucket_dir),
        reason=HISTORY_REASON_OVERWRITE,
    )
    assert entry_id is not None
    return entry_id


# ── policy ──────────────────────────────────────────────────────────────
def test_the_default_bounds_are_the_shared_policy(state_dir) -> None:
    bounds = history_maintenance.default_bounds()

    assert bounds.retention_days == HISTORY_RETENTION_DAYS_DEFAULT
    assert bounds.max_bytes == share_of_volume_bytes(
        state_dir,
        percent=HISTORY_QUOTA_PERCENT_DEFAULT,
        floor_mb=HISTORY_QUOTA_FLOOR_MB_DEFAULT,
    )


# ── sweep ───────────────────────────────────────────────────────────────
def test_the_sweep_drops_an_entry_past_the_window(state_dir, bucket_dir) -> None:
    entry_id = _capture(bucket_dir)
    age_entry(state_dir, "photos", entry_id, 1)

    report = history_maintenance.prune_now()

    assert report.removed == 1
    assert history_mod.entries("photos") == []


def test_the_sweep_keeps_a_fresh_entry(state_dir, bucket_dir) -> None:
    _capture(bucket_dir)

    report = history_maintenance.prune_now()

    assert report.removed == 0
    assert len(history_mod.entries("photos")) == 1


def test_explicit_bounds_override_the_policy(state_dir, bucket_dir) -> None:
    _capture(bucket_dir)
    empty = history_maintenance.PruneBounds(retention_days=7, max_bytes=0)

    dry = history_maintenance.prune_now(empty, dry_run=True)

    assert dry.removed == 1
    assert len(history_mod.entries("photos")) == 1
    assert history_maintenance.prune_now(empty).removed == 1
    assert history_mod.entries("photos") == []


# ── background loop ─────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_the_periodic_sweep_prunes_and_stops(state_dir, bucket_dir) -> None:
    entry_id = _capture(bucket_dir)
    age_entry(state_dir, "photos", entry_id, 1)
    stop = asyncio.Event()
    task = asyncio.create_task(history_maintenance.run_periodic_prune(stop, interval_s=0.01))

    await asyncio.sleep(0.1)
    stop.set()
    await asyncio.wait_for(task, timeout=5)

    assert task.done()
    assert history_mod.entries("photos") == []


@pytest.mark.asyncio
async def test_a_failing_sweep_does_not_end_the_loop(state_dir, monkeypatch) -> None:
    calls: list[int] = []

    def _boom() -> None:
        calls.append(1)
        raise OSError("store unreadable")

    monkeypatch.setattr(history_maintenance, "prune_now", _boom)
    stop = asyncio.Event()
    task = asyncio.create_task(history_maintenance.run_periodic_prune(stop, interval_s=0.01))

    await asyncio.sleep(0.1)
    assert not task.done()  # still sweeping after the failure
    assert len(calls) >= 1

    stop.set()
    await asyncio.wait_for(task, timeout=5)
    assert task.done()
