# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Tests for the server state-DB maintenance module (ADR-0025)."""

from unittest.mock import patch

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
