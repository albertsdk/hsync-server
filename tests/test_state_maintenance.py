# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Tests for the server state-DB maintenance module (ADR-0025)."""

from unittest.mock import MagicMock, patch

import pytest

from hsync_server import state_maintenance as sm


@pytest.fixture
def one_db(tmp_path):
    """A single per-bucket DB path the maintenance enumerates."""
    return [tmp_path / "photos.db"]


class TestVacuumOnStartup:
    def test_skips_when_freelist_below_threshold(self, one_db):
        with (
            patch.object(sm, "_server_state_db_files", return_value=one_db),
            patch.object(sm, "state_db_freelist_count", return_value=0),
            patch.object(sm, "vacuum_state_db") as mock_vacuum,
        ):
            sm.vacuum_on_startup()
            mock_vacuum.assert_not_called()

    def test_runs_when_freelist_above_threshold(self, one_db):
        with (
            patch.object(sm, "_server_state_db_files", return_value=one_db),
            patch.object(
                sm,
                "state_db_freelist_count",
                return_value=sm._STARTUP_VACUUM_MIN_FREELIST_PAGES,
            ),
            patch.object(sm, "vacuum_state_db") as mock_vacuum,
        ):
            sm.vacuum_on_startup()
            mock_vacuum.assert_called_once_with(one_db[0])

    def test_no_files_is_a_noop(self):
        with (
            patch.object(sm, "_server_state_db_files", return_value=[]),
            patch.object(sm, "vacuum_state_db") as mock_vacuum,
        ):
            sm.vacuum_on_startup()
            mock_vacuum.assert_not_called()


class TestVacuumLock:
    def test_lock_is_exclusive_and_releasable(self, tmp_path):
        db = tmp_path / "photos.db"
        fd = sm._acquire_vacuum_lock(db)
        assert fd is not None
        try:
            # A second acquisition in the same process must fail while held.
            assert sm._acquire_vacuum_lock(db) is None
        finally:
            sm._release_vacuum_lock(fd)
        # After release the lock is reacquirable.
        fd2 = sm._acquire_vacuum_lock(db)
        assert fd2 is not None
        sm._release_vacuum_lock(fd2)


class TestCloseSharedState:
    """The process-wide SQLite handles are released on shutdown (ADR-0025)."""

    def test_closes_every_cached_connection(self):
        """The cached handle is closed, and the next use re-opens instead of failing."""
        import sqlite3

        from hsync_common.models.sqlstore import get_state_db

        from hsync_server.config import get_state_db_path

        path = get_state_db_path("close-state-probe")
        conn = get_state_db(path).conn

        sm.close_shared_state()

        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")
        # Dropped, not poisoned: the cache is empty, so the next user re-opens.
        assert get_state_db(path).conn.execute("SELECT 1").fetchone()[0] == 1

    def test_is_idempotent(self):
        """A second close — lifespan, then ``atexit`` — is harmless."""
        sm.close_shared_state()
        sm.close_shared_state()

    def test_lifespan_releases_the_handles_even_when_the_final_vacuum_fails(self):
        """Closing sits in a ``finally``: a failing VACUUM must not leak the handles."""
        import asyncio
        import sqlite3

        from fastapi import FastAPI
        from hsync_common.models.sqlstore import get_state_db

        from hsync_server import main as app_main
        from hsync_server.config import get_state_db_path

        store = MagicMock()
        store.list_accounts.return_value = []

        async def _maintenance(stop: asyncio.Event) -> None:
            await stop.wait()

        captured: dict[str, sqlite3.Connection] = {}

        async def _serve() -> None:
            async with app_main.lifespan(FastAPI()):
                captured["conn"] = get_state_db(get_state_db_path("probe")).conn

        with (
            # Inert boot: the shutdown sequence is what this test pins.
            patch.object(app_main, "get_auth_store", return_value=store),
            patch.object(app_main, "register_sha256_cache_provider"),
            patch.object(app_main, "set_sha256_hashes", return_value=0),
            patch.object(app_main, "vacuum_on_startup"),
            patch.object(app_main, "run_periodic_incremental_vacuum", _maintenance),
            patch.object(app_main, "vacuum_on_shutdown", side_effect=OSError("disk gone")),
        ):
            with pytest.raises(OSError):
                asyncio.run(_serve())

        with pytest.raises(sqlite3.ProgrammingError):
            captured["conn"].execute("SELECT 1")
