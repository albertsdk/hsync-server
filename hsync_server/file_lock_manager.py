# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

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

The lock manager is thread-safe and coroutine-safe. Locks are created on-demand
and never explicitly released (they remain in the dictionary for reuse). This is
acceptable because the number of unique file paths is bounded by the number of
concurrent operations, which is limited by the rate limiter and connection pool.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


class FileLockManager:
    """
    Singleton manager for per-file asyncio.Lock instances.

    Maintains a dictionary mapping file paths to Lock objects. Locks are created
    on-demand when first acquired and remain in the dictionary for reuse. This
    ensures that all coroutines operating on the same file path contend for the
    same lock.

    Thread-safety: The dictionary itself is protected by an asyncio.Lock
    (_dict_lock) to prevent race conditions when creating new locks. Individual
    file locks are standard asyncio.Lock instances.
    """

    def __init__(self) -> None:
        """Initialize an empty lock dictionary and a dictionary-level lock."""
        self._locks: dict[str, asyncio.Lock] = {}
        self._dict_lock = asyncio.Lock()

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
        # Get or create the lock for this file path
        async with self._dict_lock:
            if file_path not in self._locks:
                self._locks[file_path] = asyncio.Lock()
            lock = self._locks[file_path]

        # Acquire the file-specific lock
        async with lock:
            yield


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
