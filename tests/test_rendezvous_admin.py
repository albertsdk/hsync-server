# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Rendezvous admin token delivery.

The generated token is a full admin credential: it must never be written to a
captured stream (systemd journal, container log driver). Interactive terminals
get it on stderr; daemons get a 0600 file under the state dir.
"""

import io
import stat

from hsync_server.auth_store.store import AuthStore


class _TtyStderr(io.StringIO):
    """StringIO that reports itself as an interactive terminal."""

    def isatty(self) -> bool:
        return True


def _store(tmp_path) -> AuthStore:
    return AuthStore(str(tmp_path / "auth.db"))


def test_generated_token_is_written_to_0600_file_when_not_a_tty(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr("hsync_server.config.get_state_dir", lambda: str(state_dir))
    monkeypatch.setattr("sys.stderr", io.StringIO())  # isatty() -> False

    token = _store(tmp_path).rendezvous_admin(None)

    assert token is not None
    token_file = state_dir / "rendezvous_admin_token"
    assert token_file.read_text(encoding="utf-8").strip() == token
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600


def test_generated_token_is_printed_on_a_tty(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr("hsync_server.config.get_state_dir", lambda: str(state_dir))
    stderr = _TtyStderr()
    monkeypatch.setattr("sys.stderr", stderr)

    token = _store(tmp_path).rendezvous_admin(None)

    assert token is not None
    assert token in stderr.getvalue()
    assert not (state_dir / "rendezvous_admin_token").exists()
