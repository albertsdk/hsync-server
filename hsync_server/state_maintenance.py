# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
State-DB maintenance for HSYNC Server (ADR-0025).

The server prunes version/tombstone rows on every mutation, but without
auto-vacuum the freed pages would linger in the per-bucket DBs forever. This
module runs a lightweight ``incremental_vacuum`` on a fixed cadence while the
server is serving (offloaded to a worker thread so the event loop never blocks)
and a full ``VACUUM`` at the startup/shutdown process boundaries where no
request is in flight. Only the DBs this server owns are touched — every
``<state_dir>/server/*.db`` (one per bucket, plus the global ``auth.db``) — never
a shared client file.

Two deployment guards keep the full VACUUM cheap in production:

- **Startup gate** — the startup VACUUM only runs when a DB's freelist holds at
  least ``_STARTUP_VACUUM_MIN_FREELIST_PAGES`` free pages, so a clean restart
  never pays a redundant full-file rewrite.
- **Single-flight lock** — a non-blocking ``flock`` on a sidecar file next to
  each DB makes the boundary VACUUM run in exactly one uvicorn worker when
  ``HSYNC_WORKERS > 1``; the others skip immediately instead of serializing on
  SQLite's ``busy_timeout``.
"""

from __future__ import annotations

import asyncio
import fcntl
import os
import time
from pathlib import Path

from hsync_common.constants import SERVER_STATE_SUBDIR
from hsync_common.models.sqlstore import (
    INCREMENTAL_VACUUM_INTERVAL_S,
    INCREMENTAL_VACUUM_PAGES,
    incremental_vacuum_state_db,
    state_db_freelist_count,
    vacuum_state_db,
)
from loguru import logger as log

from hsync_server.config import get_state_dir

__all__ = [
    "run_periodic_incremental_vacuum",
    "vacuum_on_shutdown",
    "vacuum_on_startup",
]

# A startup VACUUM below this many free pages is not worth a full rebuild
# (4 KiB pages, so 1000 pages ≈ 4 MiB). Only crash recovery is expected to
# exceed it; clean shutdowns reclaim to zero on the way out.
_STARTUP_VACUUM_MIN_FREELIST_PAGES = 1000


def _server_state_db_files() -> list[Path]:
    """Return the server's state DB files (per-bucket + auth), if any."""
    directory = Path(get_state_dir()) / SERVER_STATE_SUBDIR
    if not directory.is_dir():
        return []
    try:
        return sorted(p for p in directory.glob("*.db") if p.is_file())
    except OSError:  # pragma: no cover - defensive
        return []


def _vacuum_lock_path(db: Path) -> Path:
    """Return the sidecar lock-file path guarding the boundary VACUUM for *db*."""
    return db.with_name(db.name + ".vacuum.lock")


def _acquire_vacuum_lock(db: Path) -> int | None:
    """Acquire a non-blocking exclusive flock for *db*; return its fd, else None."""
    lock_path = _vacuum_lock_path(db)
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:  # pragma: no cover - defensive
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def _release_vacuum_lock(fd: int) -> None:
    """Release and close a lock returned by :func:`_acquire_vacuum_lock`."""
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _full_vacuum(when: str) -> None:
    """Run a full VACUUM at a process boundary, single-flight per DB and logged."""
    for db in _server_state_db_files():
        fd = _acquire_vacuum_lock(db)
        if fd is None:
            log.debug(f"State DB VACUUM ({when}) skipped: another worker holds the lock")
            continue
        try:
            start = time.monotonic()
            try:
                freed = vacuum_state_db(db)
            except Exception as exc:  # pragma: no cover - defensive, DB must never break the server
                log.debug(f"State DB VACUUM ({when}) skipped: {exc}")
                continue
            if freed:
                log.info(
                    f"State DB VACUUM ({when}) completed: reclaimed {freed} pages "
                    f"in {time.monotonic() - start:.3f}s"
                )
        finally:
            _release_vacuum_lock(fd)


def vacuum_on_startup() -> None:
    """Reclaim free space left by a previous (possibly crashed) session.

    Skipped per DB when the freelist is already small (clean restart) so startup
    is never delayed by a redundant full rebuild.
    """
    for db in _server_state_db_files():
        try:
            freelist = state_db_freelist_count(db)
        except Exception as exc:  # pragma: no cover - defensive
            log.debug(f"State DB freelist probe skipped: {exc}")
            continue
        if freelist < _STARTUP_VACUUM_MIN_FREELIST_PAGES:
            log.debug(f"State DB VACUUM (startup) skipped: freelist={freelist} below threshold")
            continue
        fd = _acquire_vacuum_lock(db)
        if fd is None:
            continue
        try:
            try:
                vacuum_state_db(db)
            except Exception as exc:  # pragma: no cover - defensive
                log.debug(f"State DB VACUUM (startup) skipped: {exc}")
        finally:
            _release_vacuum_lock(fd)


def vacuum_on_shutdown() -> None:
    """Reclaim free space accumulated during this session, at graceful shutdown."""
    _full_vacuum("shutdown")


def _incremental_vacuum_once() -> None:
    """Reclaim one bounded batch of free pages per DB, best-effort."""
    for db in _server_state_db_files():
        start = time.monotonic()
        try:
            freed = incremental_vacuum_state_db(db, INCREMENTAL_VACUUM_PAGES)
        except Exception as exc:  # pragma: no cover - defensive
            log.debug(f"State DB incremental vacuum failed: {exc}")
            continue
        if freed:
            log.info(
                f"State DB incremental vacuum reclaimed {freed} pages "
                f"in {time.monotonic() - start:.3f}s"
            )


async def run_periodic_incremental_vacuum(stop: asyncio.Event) -> None:
    """Loop that reclaims free pages every ``INCREMENTAL_VACUUM_INTERVAL_S``.

    Waits on *stop* so the lifespan can cancel it on shutdown; the SQLite call
    runs in a worker thread so the event loop is never blocked. Idempotent and
    bounded, so it is safe to run one per uvicorn worker.
    """
    while True:
        try:
            await asyncio.wait_for(stop.wait(), timeout=INCREMENTAL_VACUUM_INTERVAL_S)
            return
        except TimeoutError:
            pass
        await asyncio.to_thread(_incremental_vacuum_once)
