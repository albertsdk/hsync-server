# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""AuthStore composition and the process-wide store cache/registry."""

from __future__ import annotations

import os
import threading

from hsync_common.models.sqlstore import reset_state_db_cache

from hsync_server.config import get_auth_db_path

from .accounts import AccountsMixin
from .buckets import BucketsMixin
from .db import DbMixin
from .membership import MembershipMixin

__all__ = ["AuthStore", "get_auth_store", "reset_auth_cache"]


class AuthStore(DbMixin, AccountsMixin, MembershipMixin, BucketsMixin):
    """Account and membership store on ``state DB``.

    All mutations run inside ``BEGIN IMMEDIATE`` transactions.  scrypt hashing
    is performed **outside** transactions so the write lock is held only for
    the short SQL phase.  The operations are grouped by responsibility into
    mixins (db / accounts / membership / buckets); this class only composes
    them onto one connection-bearing instance.
    """


_pid: int | None = None
_store_cache: dict[str, AuthStore] = {}
_cache_lock = threading.Lock()


def _rebind_after_fork() -> None:
    """Drop inherited SQLite handles after a worker fork."""
    global _pid
    current = os.getpid()
    if _pid is None:
        _pid = current
        return
    if _pid != current:
        _store_cache.clear()
        reset_state_db_cache()
        _pid = current


def get_auth_store() -> AuthStore:
    """Return the cached :class:`AuthStore` for the global auth DB."""
    _rebind_after_fork()
    path = get_auth_db_path()
    with _cache_lock:
        store = _store_cache.get(path)
        if store is None:
            store = AuthStore(path)
            _store_cache[path] = store
        return store


def reset_auth_cache() -> None:
    """Clear the in-memory store cache (tests)."""
    with _cache_lock:
        _store_cache.clear()
