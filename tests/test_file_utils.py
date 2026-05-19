"""
Tests for file utility functions.
"""

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hsync_server.api import file_utils


class TestIsSentinelFile:
    """Tests for is_sentinel_file function."""

    def test_is_sentinel_file(self):
        """Test sentinel file detection."""
        from hsync_server.constants import HSYNCKEEP_FILENAME

        assert file_utils.is_sentinel_file(f"/path/to/{HSYNCKEEP_FILENAME}") is True
        assert file_utils.is_sentinel_file("/path/to/other.txt") is False


class TestEnsureSentinelInDir:
    """Tests for ensure_sentinel_in_dir function."""

    def test_creates_sentinel_in_empty_dir(self, tmp_path):
        """Sentinel is created in empty directory."""
        from hsync_server.constants import HSYNCKEEP_FILENAME

        file_utils.ensure_sentinel_in_dir(str(tmp_path))
        sentinel_path = tmp_path / HSYNCKEEP_FILENAME
        assert sentinel_path.exists()

    def test_does_not_create_sentinel_in_non_empty_dir(self, tmp_path):
        """Sentinel is not created if directory has files."""
        from hsync_server.constants import HSYNCKEEP_FILENAME

        # Create a regular file
        (tmp_path / "file.txt").write_text("content")

        file_utils.ensure_sentinel_in_dir(str(tmp_path))
        sentinel_path = tmp_path / HSYNCKEEP_FILENAME
        assert not sentinel_path.exists()

    def test_does_not_create_duplicate_sentinel(self, tmp_path):
        """Sentinel is not duplicated if already exists."""
        from hsync_server.constants import HSYNCKEEP_FILENAME

        # Create sentinel
        (tmp_path / HSYNCKEEP_FILENAME).write_text("")

        # Should not raise
        file_utils.ensure_sentinel_in_dir(str(tmp_path))

    def test_handles_nonexistent_dir(self, tmp_path):
        """Handles nonexistent directory gracefully."""
        nonexistent = tmp_path / "nonexistent"
        # Should not raise
        file_utils.ensure_sentinel_in_dir(str(nonexistent))

    def test_handles_file_instead_of_dir(self, tmp_path):
        """Handles when path is a file instead of directory."""
        file_path = tmp_path / "file.txt"
        file_path.write_text("content")

        # Should not raise
        file_utils.ensure_sentinel_in_dir(str(file_path))


class TestCleanupEmptyDir:
    """Tests for cleanup_empty_dir function."""

    def test_removes_empty_dir(self, tmp_path):
        """Empty directory is removed."""
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()

        file_utils.cleanup_empty_dir(str(empty_dir))
        assert not empty_dir.exists()

    def test_keeps_non_empty_dir(self, tmp_path):
        """Non-empty directory is kept."""
        (tmp_path / "file.txt").write_text("content")

        file_utils.cleanup_empty_dir(str(tmp_path))
        assert tmp_path.exists()

    def test_keeps_dir_with_hidden_file(self, tmp_path):
        """Directory with hidden file is kept."""
        (tmp_path / ".hidden").write_text("content")

        file_utils.cleanup_empty_dir(str(tmp_path))
        assert tmp_path.exists()

    def test_handles_nonexistent_dir(self, tmp_path):
        """Handles nonexistent directory gracefully."""
        nonexistent = tmp_path / "nonexistent"

        # Should not raise
        file_utils.cleanup_empty_dir(str(nonexistent))

    def test_handles_file_instead_of_dir(self, tmp_path):
        """Handles when path is a file instead of directory."""
        file_path = tmp_path / "file.txt"
        file_path.write_text("content")

        # Should not raise
        file_utils.cleanup_empty_dir(str(file_path))

    def test_does_not_remove_bucket_root(self, tmp_path):
        """Bucket root is preserved even when empty.

        Regression test: a malicious or legacy client could delete the
        ``.hsynckeep`` sentinel at the bucket root and trigger cleanup
        of the parent directory, which is the bucket itself. The guard
        must prevent that from wiping the whole bucket.
        """
        bucket = tmp_path / "bucket"
        bucket.mkdir()

        file_utils.cleanup_empty_dir(str(bucket), bucket_root=str(bucket))
        assert bucket.exists(), "Bucket root must not be removed"

    def test_does_not_remove_bucket_root_with_trailing_slash(self, tmp_path):
        """Trailing slashes and symlink-resolved paths still match the root guard."""
        bucket = tmp_path / "bucket"
        bucket.mkdir()

        file_utils.cleanup_empty_dir(str(bucket) + "/", bucket_root=str(bucket))
        assert bucket.exists()

    def test_still_removes_empty_subdir_with_bucket_root_arg(self, tmp_path):
        """Sub-directories within the bucket are still cleaned up."""
        bucket = tmp_path / "bucket"
        sub = bucket / "sub"
        sub.mkdir(parents=True)

        file_utils.cleanup_empty_dir(str(sub), bucket_root=str(bucket))
        assert not sub.exists()
        assert bucket.exists()


class TestIsValidPermissionFormat:
    """Tests for is_valid_permission_format function."""

    def test_valid_file_permissions(self):
        """Valid file permissions return True."""
        assert file_utils.is_valid_permission_format("-rw-r--r--") is True
        assert file_utils.is_valid_permission_format("-rwxrwxrwx") is True
        assert file_utils.is_valid_permission_format("----------") is True

    def test_valid_directory_permissions(self):
        """Valid directory permissions return True."""
        assert file_utils.is_valid_permission_format("drwxr-xr-x") is True
        assert file_utils.is_valid_permission_format("drwxrwxrwx") is True

    def test_valid_symlink_permissions(self):
        """Valid symlink permissions return True."""
        assert file_utils.is_valid_permission_format("lrwxrwxrwx") is True

    def test_invalid_permissions_wrong_length(self):
        """Wrong length returns False."""
        assert file_utils.is_valid_permission_format("rw-r--r--") is False
        assert file_utils.is_valid_permission_format("-rw-r--r--x") is False

    def test_invalid_permissions_wrong_first_char(self):
        """Wrong first character returns False."""
        assert file_utils.is_valid_permission_format("xrw-r--r--") is False
        assert file_utils.is_valid_permission_format(" rw-r--r--") is False

    def test_invalid_permissions_invalid_chars(self):
        """Invalid characters return False."""
        assert file_utils.is_valid_permission_format("-rwZrrwZrwZ") is False
        assert file_utils.is_valid_permission_format("-rw+rw+rw+") is False

    def test_empty_string(self):
        """Empty string returns False."""
        assert file_utils.is_valid_permission_format("") is False


class TestUpdateFileTimestamp:
    """Tests for update_file_timestamp function."""

    def test_update_mtime_unix_timestamp(self, tmp_path):
        """Update mtime with Unix timestamp."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("content")

        file_utils.update_file_timestamp(str(test_file), mtime="1609459200")

        # Check that file was modified (atime and mtime should be set)
        stat = test_file.stat()
        assert stat.st_mtime > 0

    def test_update_mtime_iso_format(self, tmp_path):
        """Update mtime with ISO format timestamp."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("content")

        file_utils.update_file_timestamp(str(test_file), mtime="2021-01-01T00:00:00")

        stat = test_file.stat()
        assert stat.st_mtime > 0

    def test_update_both_timestamps(self, tmp_path):
        """Update both ctime and mtime."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("content")

        file_utils.update_file_timestamp(str(test_file), ctime="1609459200", mtime="1609459200")

        stat = test_file.stat()
        assert stat.st_mtime > 0

    def test_no_timestamp_provided(self, tmp_path):
        """No timestamp update if neither provided."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("content")
        original_mtime = test_file.stat().st_mtime

        file_utils.update_file_timestamp(str(test_file))

        # Should not change (utime not called)
        assert test_file.stat().st_mtime == original_mtime
