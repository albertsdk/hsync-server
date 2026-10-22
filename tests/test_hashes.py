"""
Tests for hash management module.
"""

import os
import tempfile

import pytest

from hsync_server import hashes


class TestSHA256HashManagement:
    """Tests for SHA-256 hash storage and retrieval."""

    def setup_method(self):
        """Reset hash storage before each test."""
        hashes.FILE_HASHES = {}

    def test_set_and_get_sha256_hash(self, tmp_path):
        """Test storing and retrieving SHA-256 hash."""
        f = tmp_path / "file.txt"
        f.write_text("x")
        sha256sum = "abc123def456"

        hashes.set_sha256_hash(str(f), sha256sum)
        result = hashes.get_sha256_hash(str(f))

        assert result == sha256sum

    def test_get_nonexistent_hash(self):
        """Test retrieving non-existent hash returns None."""
        result = hashes.get_sha256_hash("/nonexistent/file.txt")
        assert result is None

    def test_set_hash_for_missing_file_is_noop(self, tmp_path):
        """A digest cannot be attached to an unreadable path: set is a no-op."""
        hashes.set_sha256_hash(str(tmp_path / "missing.txt"), "abc" * 64)
        assert len(hashes.FILE_HASHES) == 0

    def test_multiple_hashes(self, tmp_path):
        """Test storing and retrieving multiple hashes."""
        files = []
        for i, sha256sum in enumerate(("hash1", "hash2", "hash3")):
            f = tmp_path / f"file{i}.txt"
            f.write_text(f"content {i}")
            files.append((str(f), sha256sum))

        for filepath, sha256sum in files:
            hashes.set_sha256_hash(filepath, sha256sum)

        for filepath, sha256sum in files:
            assert hashes.get_sha256_hash(filepath) == sha256sum

    def test_del_sha256_hash(self, tmp_path):
        """Test deleting SHA-256 hash."""
        f = tmp_path / "file.txt"
        f.write_text("x")
        filepath = str(f)
        sha256sum = "abc123def456"

        hashes.set_sha256_hash(filepath, sha256sum)
        assert hashes.get_sha256_hash(filepath) == sha256sum

        result = hashes.del_sha256_hash(filepath)
        assert result is True
        assert hashes.get_sha256_hash(filepath) is None

    def test_del_nonexistent_hash(self):
        """Test deleting non-existent hash returns False."""
        result = hashes.del_sha256_hash("/nonexistent/file.txt")
        assert result is False

    def test_filepath_hashed_with_sha256(self, tmp_path):
        """Test that filepath is hashed (not stored plaintext)."""
        f = tmp_path / "sensitive.txt"
        f.write_text("x")
        filepath = str(f)
        sha256sum = "abc123"

        hashes.set_sha256_hash(filepath, sha256sum)

        # The filepath should not appear as a key in storage
        for key in hashes.FILE_HASHES.keys():
            assert key != filepath
            # Key should be a SHA256 hex string (64 chars)
            assert len(key) == 64

    def test_stale_entry_dropped_on_size_change(self, tmp_path):
        """Regression: a cached digest keyed only by path was served even
        after the file changed on disk. With (mtime_ns, size) captured at
        set-time, a size change must drop the entry so callers recompute."""
        f = tmp_path / "victim.txt"
        f.write_text("original")
        filepath = str(f)
        stale = "aaaa"
        hashes.set_sha256_hash(filepath, stale)

        f.write_text("a completely different content")  # size changes

        assert hashes.get_sha256_hash(filepath) is None
        assert len(hashes.FILE_HASHES) == 0  # entry evicted, not just hidden

    def test_stale_entry_dropped_on_mtime_change(self, tmp_path):
        """Same-size rewrite with a bumped mtime must also invalidate."""
        f = tmp_path / "victim.txt"
        f.write_text("same size!")
        filepath = str(f)
        stale = "bbbb"
        hashes.set_sha256_hash(filepath, stale)

        # Same-length content + explicit mtime bump (coarse-grained FS safe).
        import time as _time

        f.write_text("same leng")  # same length, different content
        new_mtime = _time.time() + 10
        import os as _os

        _os.utime(filepath, (new_mtime, new_mtime))

        assert hashes.get_sha256_hash(filepath) is None

    def test_stale_entry_dropped_when_file_vanishes(self, tmp_path):
        """A deleted file must not keep serving its old digest."""
        f = tmp_path / "victim.txt"
        f.write_text("x")
        filepath = str(f)
        hashes.set_sha256_hash(filepath, "cccc")

        f.unlink()

        assert hashes.get_sha256_hash(filepath) is None
        assert len(hashes.FILE_HASHES) == 0

    def test_cache_is_bounded_and_evicts_lru(self, tmp_path, monkeypatch):
        """The cache is capped: past the cap the least-recently-used entry goes.

        Regression: an unbounded map grew with every distinct file ever
        listed/uploaded (~360 B/entry). A recently used entry must survive the
        eviction (recency, not insertion order).
        """
        monkeypatch.setattr(hashes, "FILE_HASHES_MAX_SIZE", 3)
        files = []
        for i in range(3):
            f = tmp_path / f"f{i}.txt"
            f.write_text(f"content {i}")
            files.append(str(f))
            hashes.set_sha256_hash(str(f), f"h{i}")

        # Touch f0 so it becomes the most-recently-used entry.
        assert hashes.get_sha256_hash(files[0]) == "h0"

        f3 = tmp_path / "f3.txt"
        f3.write_text("content 3")
        hashes.set_sha256_hash(str(f3), "h3")

        assert len(hashes.FILE_HASHES) == 3
        assert hashes.get_sha256_hash(files[1]) is None  # untouched -> evicted (LRU)
        assert hashes.get_sha256_hash(files[0]) == "h0"  # recently used -> kept
        assert hashes.get_sha256_hash(files[2]) == "h2"


class TestSetSHA256Hashes:
    """Tests for set_sha256_hashes function."""

    def setup_method(self):
        """Reset hash storage before each test."""
        hashes.FILE_HASHES = {}

    def test_set_sha256_hashes_directory(self):
        """Test computing hashes for directory contents."""
        with tempfile.TemporaryDirectory() as temp_dir:
            for i in range(3):
                filepath = os.path.join(temp_dir, f"file{i}.txt")
                with open(filepath, "w") as f:
                    f.write(f"content {i}")

            count = hashes.set_sha256_hashes(temp_dir)

            assert count == 3
            assert len(hashes.FILE_HASHES) == 3

    def test_set_sha256_hashes_skips_os_cache(self):
        """Test that OS cache files (.DS_Store, Thumbs.db) are skipped."""
        with tempfile.TemporaryDirectory() as temp_dir:
            regular_file = os.path.join(temp_dir, "regular.txt")
            with open(regular_file, "w") as f:
                f.write("regular content")

            ds_store = os.path.join(temp_dir, ".DS_Store")
            with open(ds_store, "w") as f:
                f.write("cache content")
            thumbs = os.path.join(temp_dir, "Thumbs.db")
            with open(thumbs, "w") as f:
                f.write("cache content")

            count = hashes.set_sha256_hashes(temp_dir)

            assert count == 1  # Only regular file

    def test_set_sha256_hashes_includes_dotfiles(self):
        """Test that dotfiles (.env, .gitignore) are included (industry standard)."""
        with tempfile.TemporaryDirectory() as temp_dir:
            regular_file = os.path.join(temp_dir, "regular.txt")
            with open(regular_file, "w") as f:
                f.write("regular content")

            # Create dotfiles (should be included)
            env_file = os.path.join(temp_dir, ".env")
            with open(env_file, "w") as f:
                f.write("env content")
            gitignore_file = os.path.join(temp_dir, ".gitignore")
            with open(gitignore_file, "w") as f:
                f.write("git content")

            count = hashes.set_sha256_hashes(temp_dir)

            assert count == 3  # regular + .env + .gitignore

    def test_set_sha256_hashes_includes_dotfile_dirs(self):
        """Test that dotfile directories (.git/) are included (industry standard)."""
        with tempfile.TemporaryDirectory() as temp_dir:
            regular_file = os.path.join(temp_dir, "regular.txt")
            with open(regular_file, "w") as f:
                f.write("content")

            dot_dir = os.path.join(temp_dir, ".git")
            os.makedirs(dot_dir)
            dot_file = os.path.join(dot_dir, "config")
            with open(dot_file, "w") as f:
                f.write("git config")

            count = hashes.set_sha256_hashes(temp_dir)

            assert count == 2  # regular + .git/config

    def test_set_sha256_hashes_recursive(self):
        """Test recursive directory traversal."""
        with tempfile.TemporaryDirectory() as temp_dir:
            subdir = os.path.join(temp_dir, "subdir")
            os.makedirs(subdir)

            with open(os.path.join(temp_dir, "root.txt"), "w") as f:
                f.write("root")
            with open(os.path.join(subdir, "nested.txt"), "w") as f:
                f.write("nested")

            count = hashes.set_sha256_hashes(temp_dir)

            assert count == 2

    def test_set_sha256_hashes_invalid_directory(self):
        """Test error on invalid directory."""
        with pytest.raises(ValueError, match="Invalid directory path"):
            hashes.set_sha256_hashes("/nonexistent/directory")

    def test_set_sha256_hashes_resets_storage(self, tmp_path):
        """Test that set_sha256_hashes resets previous storage."""
        # Add some initial hashes (a real file is required by the cache API)
        old = tmp_path / "old.txt"
        old.write_text("old")
        hashes.set_sha256_hash(str(old), "oldhash")
        assert len(hashes.FILE_HASHES) == 1

        with tempfile.TemporaryDirectory() as temp_dir:
            filepath = os.path.join(temp_dir, "new.txt")
            with open(filepath, "w") as f:
                f.write("new content")

            hashes.set_sha256_hashes(temp_dir)

            # Old hash should be gone
            assert hashes.get_sha256_hash(str(old)) is None
            # Only new file should be present
            assert len(hashes.FILE_HASHES) == 1
