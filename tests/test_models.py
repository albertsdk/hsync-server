"""
Tests for server models (File, Dir, Event, Symlink).
"""

import os
import tempfile
import pytest
from hsync_server.models.file import File, is_hidden_file
from hsync_server.models.dir import Dir, is_hidden_dir
from hsync_server.models.event import Event, EventName
from hsync_server.models.symlink import Symlink


class TestIsHiddenFile:
    """Tests for is_hidden_file function."""

    def test_hidden_dotfile(self):
        """Dotfiles are hidden."""
        assert is_hidden_file(".gitignore") is True
        assert is_hidden_file("/path/to/.hidden") is True

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
        f = File(
            filepath="/bucket/test.txt",
            size=100,
            md5sum="abc123",
            permissions="-rw-r--r--"
        )
        assert f.filepath == "/bucket/test.txt"
        assert f.size == 100
        assert f.md5sum == "abc123"
        assert f.permissions == "-rw-r--r--"

    def test_to_dict(self):
        """Convert File to dictionary."""
        f = File(
            filepath="/bucket/test.txt",
            size=100,
            md5sum="abc123",
            permissions="-rw-r--r--"
        )
        d = f.to_dict()
        assert d["filepath"] == "/bucket/test.txt"
        assert d["size"] == 100
        assert d["md5sum"] == "abc123"

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
    """Tests for is_hidden_dir function."""

    def test_hidden_parent_dir(self):
        """Directories with hidden parent are hidden."""
        # is_hidden_dir checks if the PARENT directory starts with .
        assert is_hidden_dir("/.git/objects") is True
        assert is_hidden_dir("/path/.hidden/subdir") is True

    def test_regular_dir(self):
        """Regular directories are not hidden."""
        assert is_hidden_dir("documents") is False
        assert is_hidden_dir("/path/to/folder") is False
        # Even if dir name starts with ., parent doesn't
        assert is_hidden_dir("/path/to/.git") is False


class TestDirModel:
    """Tests for Dir model."""

    def test_init_basic(self):
        """Initialize Dir with basic parameters."""
        d = Dir(path="/bucket/subdir", permissions="drwxr-xr-x")
        assert d.path == "/bucket/subdir"
        assert d.permissions == "drwxr-xr-x"

    def test_to_dict(self):
        """Convert Dir to dictionary."""
        d = Dir(path="/bucket/subdir", permissions="drwxr-xr-x")
        result = d.to_dict()
        assert result["path"] == "/bucket/subdir"
        assert result["permissions"] == "drwxr-xr-x"

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
        f = File(filepath="/buckets/test/test.txt", size=100, md5sum="abc", permissions="-rw-r--r--")
        e = Event(
            cid="client1",
            localpath="/buckets/test",
            bucket="test",
            name=EventName.added,
            src=f
        )
        assert e.cid == "client1"
        assert e.name == EventName.added

    def test_to_dict(self):
        """Convert Event to dictionary."""
        f = File(filepath="/buckets/test/test.txt", size=100, md5sum="abc", permissions="-rw-r--r--")
        e = Event(
            cid="client1",
            localpath="/buckets/test",
            bucket="test",
            name=EventName.modified,
            src=f
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

    def test_to_dict(self):
        """Convert Symlink to dictionary."""
        s = Symlink(filepath="/bucket/link", target="/bucket/target")
        d = s.to_dict()
        assert d["filepath"] == "/bucket/link"
        assert d["target"] == "/bucket/target"
        assert d["type"] == "symlink"

