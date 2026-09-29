# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
Generic per-bucket store registry (fork-safe, thread-safe).

``versions.py`` (causal version store) and ``tombstones.py`` (deletion
markers) are per-bucket wrappers around the shared ``state DB``. Both
modules previously re-implemented the same ``_stores`` / ``_locks`` /
``_locks_guard`` / ``_pid`` scaffolding: this registry extracts it once.

- **Fork safety**: after a worker fork the inherited SQLite handles are
  invalid; ``get_or_create`` detects the PID change, drops the cache and
  resets the shared DB-handle cache before creating new stores.
- **Thread safety**: a per-bucket ``RLock`` serializes store mutations;
  creation is memoized under the same lock so two threads can never build
  two stores for the same bucket.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable

from hsync_common.models.sqlstore import get_state_db, reset_state_db_cache

from hsync_server.config import get_state_db_path


def bucket_state_identity(bucket: str) -> tuple[str, str]:
    """Return ``(db_path, folder_id)`` for *bucket*'s shared state DB.

    Resolves the per-bucket DB path and ensures its folder UUID exists (minting
    it on first use). :mod:`versions` and :mod:`tombstones` both bootstrap their
    stores from this pair, so the identity resolution lives in one place.
    """
    db_path = get_state_db_path(bucket)
    folder_id = get_state_db(db_path).ensure_bucket_id(bucket)
    return db_path, folder_id


class PerBucketStores[T]:
    """Memoized per-bucket store registry for one store type."""

    def __init__(self) -> None:
        self._stores: dict[str, T] = {}
        self._locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()
        self._pid: int | None = None

    def _rebind_after_fork(self) -> None:
        """Drop inherited SQLite handles after a worker fork."""
        current = os.getpid()
        if self._pid is None:
            self._pid = current
            return
        if self._pid != current:
            self._stores.clear()
            self._locks.clear()
            reset_state_db_cache()
            self._pid = current

    def lock(self, bucket: str) -> threading.RLock:
        """Return the per-bucket ``RLock`` used to serialize store mutations."""
        with self._locks_guard:
            lock = self._locks.get(bucket)
            if lock is None:
                lock = threading.RLock()
                self._locks[bucket] = lock
            return lock

    def get_or_create(self, bucket: str, factory: Callable[[], T]) -> T:
        """Return the cached store for *bucket*, building it on first use.

        *factory* builds a fully loaded (``.load()`` already called) store;
        it runs at most once per bucket and never concurrently with itself.
        """
        self._rebind_after_fork()
        store = self._stores.get(bucket)
        if store is None:
            with self.lock(bucket):
                store = self._stores.get(bucket)
                if store is None:
                    store = factory()
                    self._stores[bucket] = store
        return store

    def clear(self) -> None:
        """Drop every cached store and lock (tests / process teardown)."""
        self._stores.clear()
        self._locks.clear()
