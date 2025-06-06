"""
Tests for server models (File, Dir, Event, Symlink).
"""

import os
import tempfile

from hsync_common.models.dir import Dir, is_hidden_dir
from hsync_common.models.event import Event, EventName
from hsync_common.models.file import File, is_hidden_file
from hsync_common.models.symlink import Symlink


class TestIsHiddenFile:
    """Tests for is_hidden_file function.

    Following the industry standard (Syncthing, rsync, Nextcloud, OneDrive),
    dotfiles in general (.env, .config, .git …) are NOT excluded: only OS
    cache files and editor/office temporary files are filtered by default.
    """

    def test_dotfile_not_hidden(self):
        """Dotfiles are NOT hidden (industry standard)."""
        assert is_hidden_file(".gitignore") is False
        assert is_hidden_file("/path/to/.hidden") is False
        assert is_hidden_file(".env") is False

    def test_os_cache_files(self):
        """OS cache files are hidden."""
        assert is_hidden_file(".DS_Store") is True
        assert is_hidden_file("Thumbs.db") is True

    def test_backup_files(self):
        """Backup files are hidden."""
        assert is_hidden_file("file.txt~") is True
        assert is_hidden_file("file.bak") is True
        assert is_hidden_file("file.tmp") is True
        assert is_hidden_file("file.temp") is True

    def test_vim_files(self):
        """Vim temp/swap files are hidden."""
        assert is_hidden_file("4913") is True  # vim temp
        assert is_hidden_file(".file.swp") is True

    def test_regular_files(self):
        """Regular files are not hidden."""
        assert is_hidden_file("document.txt") is False
        assert is_hidden_file("image.png") is False


class TestFileModel:
    """Tests for File model."""

    def test_init_basic(self):
        """Initialize File with basic parameters."""
        f = File(filepath="/bucket/test.txt", size=100, sha256sum="abc123", permissions="644")
        assert f.filepath == "/bucket/test.txt"
        assert f.size == 100
        assert f.sha256sum == "abc123"
        assert f.permissions == "644"

    def test_to_dict(self):
        """Convert File to dictionary."""
        f = File(filepath="/bucket/test.txt", size=100, sha256sum="abc123", permissions="644")
        d = f.to_dict()
        assert d["filepath"] == "/bucket/test.txt"
        assert d["size"] == 100
        assert d["sha256sum"] == "abc123"

    def test_get_existing_file(self):
        """Get File from existing file path."""
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(b"test content")
            tmp_path = tmp.name

        try:
            f = File().get(tmp_path)
            assert f is not None
            assert f.filepath == tmp_path
            assert f.size == 12
        finally:
            os.unlink(tmp_path)

    def test_get_nonexistent_file(self):
        """Get File from non-existent path returns None."""
        f = File().get("/nonexistent/path/file.txt")
        assert f is None


class TestIsHiddenDir:
    """Tests for is_hidden_dir function.

    Following the industry standard, dotfile directories in general
    (.git/, .config/ …) are NOT excluded: only OS cache directories and
    editor/office temporary files are filtered by default.
    """

    def test_dotdir_not_hidden(self):
        """Dotfile directories are NOT hidden (industry standard)."""
        assert is_hidden_dir("/.git/objects") is False
        assert is_hidden_dir("/path/.hidden/subdir") is False
        assert is_hidden_dir("/path/to/.git") is False
        assert is_hidden_dir(".hidden") is False
        assert is_hidden_dir("/.config") is False

    def test_regular_dir(self):
        """Regular directories are not hidden."""
        assert is_hidden_dir("documents") is False
        assert is_hidden_dir("/path/to/folder") is False

    def test_internal_dir_is_hidden(self):
        """Engine-owned directories are always hidden."""
        assert is_hidden_dir(".hsyncignore") is True
        assert is_hidden_dir(".hsyncstate") is True
        assert is_hidden_dir(".hsynctombstones") is True


class TestDirModel:
    """Tests for Dir model."""

    def test_init_basic(self):
        """Initialize Dir with basic parameters."""
        d = Dir(path="/bucket/subdir", permissions="755")
        assert d.path == "/bucket/subdir"
        assert d.permissions == "755"

    def test_to_dict(self):
        """Convert Dir to dictionary."""
        d = Dir(path="/bucket/subdir", permissions="755")
        result = d.to_dict()
        assert result["path"] == "/bucket/subdir"
        assert result["permissions"] == "755"

    def test_get_existing_dir(self):
        """Get Dir from existing directory."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            d = Dir().get(tmp_dir)
            assert d is not None
            assert d.path == tmp_dir


class TestEventName:
    """Tests for EventName enum."""

    def test_event_values(self):
        """Verify enum values."""
        assert EventName.added.value == "added"
        assert EventName.modified.value == "modified"
        assert EventName.deleted.value == "deleted"
        assert EventName.renamed.value == "renamed"
        assert EventName.symlink.value == "symlink"


class TestEventModel:
    """Tests for Event model."""

    def test_init_with_file(self):
        """Initialize Event with File source."""
        f = File(filepath="/buckets/test/test.txt", size=100, sha256sum="abc", permissions="644")
        e = Event(
            cid="client1", localpath="/buckets/test", bucket="test", name=EventName.added, src=f
        )
        assert e.cid == "client1"
        assert e.name == EventName.added

    def test_to_dict(self):
        """Convert Event to dictionary."""
        f = File(filepath="/buckets/test/test.txt", size=100, sha256sum="abc", permissions="644")
        e = Event(
            cid="client1", localpath="/buckets/test", bucket="test", name=EventName.modified, src=f
        )
        d = e.to_dict()
        assert d["name"] == "modified"
        assert "src" in d


class TestSymlinkModel:
    """Tests for Symlink model."""

    def test_init_basic(self):
        """Initialize Symlink with basic parameters."""
        s = Symlink(filepath="/bucket/link", target="/bucket/target")
        assert s.filepath == "/bucket/link"
        assert s.target == "/bucket/target"
        assert s.name == "link"

    def test_init_with_all_params(self):
        """Initialize Symlink with all parameters."""
        s = Symlink(
            filepath="/path/link",
            target="/path/target",
            name="custom_name",
            ctime=1000.0,
            mtime=2000.0,
        )
        assert s.name == "custom_name"
        assert s.ctime == 1000.0
        assert s.mtime == 2000.0

    def test_repr(self):
        """Test Symlink repr."""
        s = Symlink(filepath="/path/link", target="/path/target")
        r = repr(s)
        assert "Symlink" in r
        assert "/path/link" in r
        assert "/path/target" in r

    def test_to_dict(self):
        """Convert Symlink to dictionary."""
        s = Symlink(filepath="/bucket/link", target="/bucket/target")
        d = s.to_dict()
        assert d["filepath"] == "/bucket/link"
        assert d["target"] == "/bucket/target"
        assert d["type"] == "symlink"

    def test_from_dict(self):
        """Create Symlink from dictionary."""
        data = {
            "filepath": "/path/link",
            "name": "link",
            "target": "/path/target",
            "ctime": 1000.0,
            "mtime": 2000.0,
        }
        s = Symlink.from_dict(data)
        assert s.filepath == "/path/link"
        assert s.target == "/path/target"
        assert s.ctime == 1000.0

    def test_get_existing_symlink(self, tmp_path):
        """Get Symlink from existing symlink file."""
        target = tmp_path / "target.txt"
        target.write_text("content")
        link = tmp_path / "link"
        link.symlink_to(target)

        s = Symlink.get(str(link))
        assert s is not None
        assert s.name == "link"

    def test_get_nonexistent_path(self):
        """Get returns None for nonexistent path."""
        s = Symlink.get("/nonexistent/path/link")
        assert s is None

    def test_get_regular_file(self, tmp_path):
        """Get returns None for regular file (not symlink)."""
        f = tmp_path / "regular.txt"
        f.write_text("content")
        s = Symlink.get(str(f))
        assert s is None

    def test_get_broken_symlink(self, tmp_path):
        """Get handles broken symlink gracefully."""
        link = tmp_path / "broken_link"
        link.symlink_to("/nonexistent/target")
        # Should not raise an exception
        Symlink.get(str(link))
        # Broken symlinks may return None due to stat failure


class TestTombstoneReexports:
    """Tombstone symbols are importable from hsync_common.models."""

    def test_tombstone_symbols_importable_from_package(self):
        from hsync_common.models import (
            TombstoneEntry,
            TombstoneStore,
            relpath_in_bucket,
            tombstone_key,
        )

        assert TombstoneEntry is not None
        assert TombstoneStore is not None
        assert callable(tombstone_key)
        assert callable(relpath_in_bucket)

    def test_reexports_are_canonical_common_objects(self):
        from hsync_common.models import TombstoneStore, tombstone_key
        from hsync_common.models import tombstone as common_tombstone

        assert TombstoneStore is common_tombstone.TombstoneStore
        assert tombstone_key is common_tombstone.tombstone_key
