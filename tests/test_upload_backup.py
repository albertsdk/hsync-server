"""Tests for the server overwrite backup helper (hard link with copy fallback)."""

import os

from hsync_server.api.files import upload as upload_mod
from hsync_server.api.files.upload import _backup_existing_file


class TestBackupExistingFile:
    def test_prefers_hard_link(self, tmp_path):
        src = tmp_path / "orig.bin"
        src.write_bytes(b"previous content")
        backup = tmp_path / "orig.bin.backup"

        _backup_existing_file(str(src), str(backup))

        assert backup.read_bytes() == b"previous content"
        assert backup.stat().st_ino == src.stat().st_ino

        # Publishing a new inode (the upload path's atomic replace) must leave
        # the snapshot untouched.
        new = tmp_path / "new.bin"
        new.write_bytes(b"new content")
        os.replace(new, src)
        assert backup.read_bytes() == b"previous content"
        assert src.read_bytes() == b"new content"

    def test_falls_back_to_copy_when_link_unsupported(self, tmp_path, monkeypatch):
        src = tmp_path / "orig.bin"
        src.write_bytes(b"previous content")
        backup = tmp_path / "orig.bin.backup"

        def _no_link(*args, **kwargs):
            raise OSError("links unsupported")

        monkeypatch.setattr(upload_mod, "link", _no_link)
        _backup_existing_file(str(src), str(backup))

        assert backup.read_bytes() == b"previous content"
        assert backup.stat().st_ino != src.stat().st_ino

    def test_stale_backup_is_replaced(self, tmp_path):
        src = tmp_path / "orig.bin"
        src.write_bytes(b"previous content")
        backup = tmp_path / "orig.bin.backup"
        backup.write_bytes(b"stale")

        # A pre-existing backup makes link fail with FileExistsError; the copy
        # fallback overwrites it with the current content.
        _backup_existing_file(str(src), str(backup))

        assert backup.read_bytes() == b"previous content"

    def test_stale_self_link_is_reused(self, tmp_path):
        src = tmp_path / "orig.bin"
        src.write_bytes(b"previous content")
        backup = tmp_path / "orig.bin.backup"
        # Simulate a crash between the link and the atomic publish: the stale
        # snapshot is the same inode as the target.
        os.link(src, backup)

        _backup_existing_file(str(src), str(backup))

        # The existing link is already a valid snapshot: no SameFileError, and
        # the target must survive.
        assert backup.read_bytes() == b"previous content"
        assert backup.stat().st_ino == src.stat().st_ino
        assert src.read_bytes() == b"previous content"
