# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""Tests for the per-bucket sync-state DB layout (ADR-0025)."""

from pathlib import Path

from hsync_server.config import get_auth_db_path, get_state_db_path


class TestServerStateLayout:
    def test_per_bucket_paths_are_isolated(self):
        a = get_state_db_path("alpha")
        b = get_state_db_path("beta")
        assert a != b
        assert Path(a).name == "alpha.db"
        assert Path(a).parent.name == "server"
        assert Path(b).name == "beta.db"

    def test_auth_db_is_global_and_separate(self):
        auth = get_auth_db_path()
        assert Path(auth).name == "auth.db"
        assert Path(auth).parent.name == "server"
        assert auth != get_state_db_path("alpha")

    def test_state_db_override_forces_a_single_db(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HSYNC_STATE_DB", str(tmp_path / "one.db"))
        assert get_state_db_path("alpha") == str(tmp_path / "one.db")
        assert get_state_db_path("beta") == str(tmp_path / "one.db")
        # Auth is never overridden to a per-process single file.
        assert Path(get_auth_db_path()).name == "auth.db"
