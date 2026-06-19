# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Tests for the server state-DB maintenance module (ADR-0025)."""

from unittest.mock import patch

from hsync_server import state_maintenance as sm


class TestVacuumOnStartup:
    def test_skips_when_freelist_below_threshold(self):
        with (
            patch.object(sm, "state_db_freelist_count", return_value=0),
            patch.object(sm, "vacuum_state_db") as mock_vacuum,
        ):
            sm.vacuum_on_startup()
            mock_vacuum.assert_not_called()

    def test_runs_when_freelist_above_threshold(self):
        with (
            patch.object(
                sm, "state_db_freelist_count", return_value=sm._STARTUP_VACUUM_MIN_FREELIST_PAGES
            ),
            patch.object(sm, "vacuum_state_db") as mock_vacuum,
        ):
            sm.vacuum_on_startup()
            mock_vacuum.assert_called_once()


class TestVacuumLock:
    def test_lock_is_exclusive_and_releasable(self):
        fd = sm._acquire_vacuum_lock()
        assert fd is not None
        try:
            # A second acquisition in the same process must fail while held.
            assert sm._acquire_vacuum_lock() is None
        finally:
            sm._release_vacuum_lock(fd)
        # After release the lock is reacquirable.
        fd2 = sm._acquire_vacuum_lock()
        assert fd2 is not None
        sm._release_vacuum_lock(fd2)
