# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""
Tests for the per-file lock manager.

Covers the two invariants that matter: a path's lock actually serializes its
contenders (including while the registry is evicting), and the registry stays
bounded under a large population of distinct paths.
"""

import asyncio

import pytest

from hsync_server.file_lock_manager import FileLockManager, get_file_lock_manager


async def test_same_path_serializes() -> None:
    """Two coroutines on the same path must not interleave in the critical section."""
    manager = FileLockManager(max_locks=16)
    events: list[str] = []

    async def worker(tag: str) -> None:
        async with manager.acquire("/bucket/report.bin"):
            events.append(f"enter:{tag}")
            await asyncio.sleep(0.01)
            events.append(f"exit:{tag}")

    await asyncio.gather(worker("a"), worker("b"))

    # Each enter is immediately followed by its own exit: no interleaving.
    assert [e.split(":")[0] for e in events] == ["enter", "exit", "enter", "exit"]
    assert events[0].split(":")[1] == events[1].split(":")[1]
    assert events[2].split(":")[1] == events[3].split(":")[1]


async def test_distinct_paths_do_not_serialize() -> None:
    """Different paths use different locks and may run concurrently."""
    manager = FileLockManager(max_locks=16)
    running = 0
    peak = 0

    async def worker(path: str) -> None:
        nonlocal running, peak
        async with manager.acquire(path):
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.01)
            running -= 1

    await asyncio.gather(*(worker(f"/bucket/f{i}.bin") for i in range(5)))

    assert peak > 1


async def test_registry_is_bounded() -> None:
    """Touching far more distinct paths than the cap must not grow the map."""
    manager = FileLockManager(max_locks=64)

    for i in range(500):
        async with manager.acquire(f"/bucket/f{i}.bin"):
            pass

    assert len(manager._locks) <= 64


async def test_eviction_never_drops_a_held_lock() -> None:
    """Under a one-entry cap, contenders on one path must still serialize."""
    manager = FileLockManager(max_locks=1)
    concurrent = 0
    peak = 0

    async def worker() -> None:
        nonlocal concurrent, peak
        async with manager.acquire("/bucket/hot.bin"):
            concurrent += 1
            peak = max(peak, concurrent)
            await asyncio.sleep(0.005)
            concurrent -= 1

    await asyncio.gather(*(worker() for _ in range(8)))

    assert peak == 1
    assert len(manager._locks) == 1


async def test_claim_released_on_exception() -> None:
    """A failed critical section must release its claim and the lock."""
    manager = FileLockManager(max_locks=8)

    with pytest.raises(RuntimeError):
        async with manager.acquire("/bucket/f.bin"):
            raise RuntimeError("boom")

    assert manager._locks["/bucket/f.bin"].claimants == 0
    async with manager.acquire("/bucket/f.bin"):
        pass


def test_singleton_is_shared() -> None:
    """The accessor returns one process-wide manager."""
    assert get_file_lock_manager() is get_file_lock_manager()
