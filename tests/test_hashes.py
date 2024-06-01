"""
Tests for hash management module.
"""

import os
import tempfile

import pytest

from hsync_server import hashes


class TestMD5HashManagement:
    """Tests for MD5 hash storage and retrieval."""

    def setup_method(self):
        """Reset hash storage before each test."""
        hashes.FILE_HASHES = []

    def test_set_and_get_md5_hash(self):
        """Test storing and retrieving MD5 hash."""
        filepath = "/path/to/file.txt"
        md5sum = "abc123def456"

        hashes.set_md5_hash(filepath, md5sum)
        result = hashes.get_md5_hash(filepath)

        assert result == md5sum

    def test_get_nonexistent_hash(self):
        """Test retrieving non-existent hash returns None."""
        result = hashes.get_md5_hash("/nonexistent/file.txt")
        assert result is None

    def test_multiple_hashes(self):
        """Test storing and retrieving multiple hashes."""
        files = [
            ("/path/file1.txt", "hash1"),
            ("/path/file2.txt", "hash2"),
            ("/path/file3.txt", "hash3"),
        ]

        for filepath, md5sum in files:
            hashes.set_md5_hash(filepath, md5sum)

        for filepath, md5sum in files:
            assert hashes.get_md5_hash(filepath) == md5sum

    def test_del_md5_hash(self):
        """Test deleting MD5 hash."""
        filepath = "/path/to/file.txt"
        md5sum = "abc123def456"

        hashes.set_md5_hash(filepath, md5sum)
        assert hashes.get_md5_hash(filepath) == md5sum

        result = hashes.del_md5_hash(filepath)
        assert result is True
        assert hashes.get_md5_hash(filepath) is None

    def test_del_nonexistent_hash(self):
        """Test deleting non-existent hash returns False."""
        result = hashes.del_md5_hash("/nonexistent/file.txt")
        assert result is False

    def test_filepath_hashed_with_sha256(self):
        """Test that filepath is hashed (not stored plaintext)."""
        filepath = "/sensitive/path/file.txt"
        md5sum = "abc123"

        hashes.set_md5_hash(filepath, md5sum)

        # The filepath should not appear in the storage
        for entry in hashes.FILE_HASHES:
            for key in entry.keys():
                assert key != filepath
                # Key should be a SHA256 hex string (64 chars)
                assert len(key) == 64


class TestSetMD5Hashes:
    """Tests for set_md5_hashes function."""

    def setup_method(self):
        """Reset hash storage before each test."""
        hashes.FILE_HASHES = []

    def test_set_md5_hashes_directory(self):
        """Test computing hashes for directory contents."""
        with tempfile.TemporaryDirectory() as temp_dir:
            # Create some test files
            for i in range(3):
                filepath = os.path.join(temp_dir, f"file{i}.txt")
                with open(filepath, "w") as f:
                    f.write(f"content {i}")

            count = hashes.set_md5_hashes(temp_dir)

            assert count == 3
            assert len(hashes.FILE_HASHES) == 3

    def test_set_md5_hashes_skips_hidden(self):
        """Test that hidden files are skipped."""
        with tempfile.TemporaryDirectory() as temp_dir:
            # Create regular file
            regular_file = os.path.join(temp_dir, "regular.txt")
            with open(regular_file, "w") as f:
                f.write("regular content")

            # Create hidden file
            hidden_file = os.path.join(temp_dir, ".hidden")
            with open(hidden_file, "w") as f:
                f.write("hidden content")

            count = hashes.set_md5_hashes(temp_dir)

            assert count == 1  # Only regular file

    def test_set_md5_hashes_skips_hidden_dirs(self):
        """Test that hidden directories are skipped."""
        with tempfile.TemporaryDirectory() as temp_dir:
            # Create regular file in root
            regular_file = os.path.join(temp_dir, "regular.txt")
            with open(regular_file, "w") as f:
                f.write("content")

            # Create hidden directory with file inside
            hidden_dir = os.path.join(temp_dir, ".hidden")
            os.makedirs(hidden_dir)
            hidden_file = os.path.join(hidden_dir, "file.txt")
            with open(hidden_file, "w") as f:
                f.write("hidden content")

            count = hashes.set_md5_hashes(temp_dir)

            assert count == 1  # Only regular file in root

    def test_set_md5_hashes_recursive(self):
        """Test recursive directory traversal."""
        with tempfile.TemporaryDirectory() as temp_dir:
            # Create nested structure
            subdir = os.path.join(temp_dir, "subdir")
            os.makedirs(subdir)

            with open(os.path.join(temp_dir, "root.txt"), "w") as f:
                f.write("root")
            with open(os.path.join(subdir, "nested.txt"), "w") as f:
                f.write("nested")

            count = hashes.set_md5_hashes(temp_dir)

            assert count == 2

    def test_set_md5_hashes_invalid_directory(self):
        """Test error on invalid directory."""
        with pytest.raises(ValueError, match="Invalid directory path"):
            hashes.set_md5_hashes("/nonexistent/directory")

    def test_set_md5_hashes_resets_storage(self):
        """Test that set_md5_hashes resets previous storage."""
        # Add some initial hashes
        hashes.set_md5_hash("/old/file.txt", "oldhash")
        assert len(hashes.FILE_HASHES) == 1

        with tempfile.TemporaryDirectory() as temp_dir:
            filepath = os.path.join(temp_dir, "new.txt")
            with open(filepath, "w") as f:
                f.write("new content")

            hashes.set_md5_hashes(temp_dir)

            # Old hash should be gone
            assert hashes.get_md5_hash("/old/file.txt") is None
            # Only new file should be present
            assert len(hashes.FILE_HASHES) == 1
