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

    def test_set_and_get_sha256_hash(self):
        """Test storing and retrieving SHA-256 hash."""
        filepath = "/path/to/file.txt"
        sha256sum = "abc123def456"

        hashes.set_sha256_hash(filepath, sha256sum)
        result = hashes.get_sha256_hash(filepath)

        assert result == sha256sum

    def test_get_nonexistent_hash(self):
        """Test retrieving non-existent hash returns None."""
        result = hashes.get_sha256_hash("/nonexistent/file.txt")
        assert result is None

    def test_multiple_hashes(self):
        """Test storing and retrieving multiple hashes."""
        files = [
            ("/path/file1.txt", "hash1"),
            ("/path/file2.txt", "hash2"),
            ("/path/file3.txt", "hash3"),
        ]

        for filepath, sha256sum in files:
            hashes.set_sha256_hash(filepath, sha256sum)

        for filepath, sha256sum in files:
            assert hashes.get_sha256_hash(filepath) == sha256sum

    def test_del_sha256_hash(self):
        """Test deleting SHA-256 hash."""
        filepath = "/path/to/file.txt"
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

    def test_filepath_hashed_with_sha256(self):
        """Test that filepath is hashed (not stored plaintext)."""
        filepath = "/sensitive/path/file.txt"
        sha256sum = "abc123"

        hashes.set_sha256_hash(filepath, sha256sum)

        # The filepath should not appear as a key in storage
        for key in hashes.FILE_HASHES.keys():
            assert key != filepath
            # Key should be a SHA256 hex string (64 chars)
            assert len(key) == 64


class TestSetSHA256Hashes:
    """Tests for set_sha256_hashes function."""

    def setup_method(self):
        """Reset hash storage before each test."""
        hashes.FILE_HASHES = {}

    def test_set_sha256_hashes_directory(self):
        """Test computing hashes for directory contents."""
        with tempfile.TemporaryDirectory() as temp_dir:
            # Create some test files
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
            # Create regular file
            regular_file = os.path.join(temp_dir, "regular.txt")
            with open(regular_file, "w") as f:
                f.write("regular content")

            # Create OS cache files
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
            # Create regular file
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
            # Create regular file in root
            regular_file = os.path.join(temp_dir, "regular.txt")
            with open(regular_file, "w") as f:
                f.write("content")

            # Create dotfile directory with file inside
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
            # Create nested structure
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

    def test_set_sha256_hashes_resets_storage(self):
        """Test that set_sha256_hashes resets previous storage."""
        # Add some initial hashes
        hashes.set_sha256_hash("/old/file.txt", "oldhash")
        assert len(hashes.FILE_HASHES) == 1

        with tempfile.TemporaryDirectory() as temp_dir:
            filepath = os.path.join(temp_dir, "new.txt")
            with open(filepath, "w") as f:
                f.write("new content")

            hashes.set_sha256_hashes(temp_dir)

            # Old hash should be gone
            assert hashes.get_sha256_hash("/old/file.txt") is None
            # Only new file should be present
            assert len(hashes.FILE_HASHES) == 1
