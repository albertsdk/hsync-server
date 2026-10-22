# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""
Per-file lock manager for serializing concurrent operations on the same file.

This module provides a singleton lock manager that maintains a dictionary of
asyncio.Lock instances keyed by file path. It is used to serialize operations
that would otherwise race on the same file (e.g., concurrent delta applies
writing to the same `.delta.part` file).

Usage:
    from hsync_server.file_lock_manager import get_file_lock_manager

    lock_manager = get_file_lock_manager()
    async with lock_manager.acquire(file_path):
        # Critical section: only one coroutine can execute this for the same file_path
        ...

The registry is **bounded**. It is keyed by every distinct path that has ever
been uploaded or delta-applied, so a never-released map grows with the whole
file history of the server (one asyncio.Lock plus its absolute-path key per
file, ~240 bytes each measured — hundreds of MB on a large bucket). Idle,
unreferenced locks are evicted once the ceiling is reached instead. A lock is
never evicted while a coroutine still references it, so two coroutines can never
end up contending on different lock objects for the same path.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass


@dataclass(slots=True)
class _LockEntry:
    """A per-path lock plus how many coroutines have claimed it."""

    lock: asyncio.Lock
    claimants: int = 0


class FileLockManager:
    """
    Singleton manager for per-file asyncio.Lock instances.

    Maintains a dictionary mapping file paths to Lock objects. Locks are created
    on-demand when first acquired and reused while claimed; idle, unreferenced
    locks are evicted to keep the map bounded (see the module docstring).

    Thread-safety: The dictionary itself is protected by an asyncio.Lock
    (_dict_lock) to prevent race conditions when creating new locks. Individual
    file locks are standard asyncio.Lock instances.
    """

    # Ceiling on retained locks. Live contention is bounded by the rate limiter
    # and connection pool (a handful of paths at once), so this is generous
    # headroom while capping RAM at roughly 8192 * ~240 B ≈ 2 MB.
    DEFAULT_MAX_LOCKS = 8192

    def __init__(self, max_locks: int = DEFAULT_MAX_LOCKS) -> None:
        """Initialize an empty lock dictionary and a dictionary-level lock."""
        # path -> _LockEntry; insertion order drives idle eviction (the oldest
        # entry is the best eviction candidate).
        self._locks: dict[str, _LockEntry] = {}
        self._dict_lock = asyncio.Lock()
        self._max_locks = max_locks

    def _evict_idle(self) -> None:
        """Drop the oldest idle, unreferenced lock once the ceiling is reached.

        Called with ``_dict_lock`` held. Only an entry with no claimant and not
        currently held is dropped, so the eviction can never let two coroutines
        contend on different lock objects for the same path.
        """
        if len(self._locks) < self._max_locks:
            return
        victim: str | None = None
        for path, entry in self._locks.items():
            if entry.claimants == 0 and not entry.lock.locked():
                victim = path
                break
        if victim is not None:
            del self._locks[victim]

    @asynccontextmanager
    async def acquire(self, file_path: str) -> AsyncIterator[None]:
        """
        Acquire the lock for the given file path.

        This is an async context manager. The lock is created on-demand if it
        does not exist, then acquired. The lock is released when the context
        manager exits.

        Args:
            file_path: The file path to lock on. Should be an absolute path
                      or a path unique within the server's scope.

        Yields:
            None. The caller executes the critical section in the `async with` block.

        Example:
            async with lock_manager.acquire("/path/to/file"):
                # Only one coroutine can be here for the same file_path
                await do_something_with_file()
        """
        async with self._dict_lock:
            entry = self._locks.get(file_path)
            if entry is None:
                entry = _LockEntry(lock=asyncio.Lock())
                self._locks[file_path] = entry
            # Claim before eviction (so this entry can never be the victim) and
            # before releasing the dict lock (so a concurrent acquire for the
            # same path always observes the same lock object).
            entry.claimants += 1
            self._evict_idle()
            lock = entry.lock
        try:
            async with lock:
                yield
        finally:
            async with self._dict_lock:
                current = self._locks.get(file_path)
                if current is not None and current.lock is lock:
                    current.claimants -= 1


# Singleton instance
_file_lock_manager: FileLockManager | None = None


def get_file_lock_manager() -> FileLockManager:
    """
    Get the singleton FileLockManager instance.

    Returns:
        The singleton FileLockManager instance.
    """
    global _file_lock_manager
    if _file_lock_manager is None:
        _file_lock_manager = FileLockManager()
    return _file_lock_manager
