"""
Tests for API utility functions.
"""

import os
import tempfile
from datetime import datetime, timezone

import pytest

from hsync_server.api.file_utils import (
    is_valid_permission_format,
    update_file_timestamp,
)


class TestIsValidPermissionFormat:
    """Tests for is_valid_permission_format function."""

    def test_valid_file_permissions(self):
        """Valid file permission formats."""
        assert is_valid_permission_format("-rw-r--r--") is True
        assert is_valid_permission_format("-rwxr-xr-x") is True
        assert is_valid_permission_format("-rwxrwxrwx") is True
        assert is_valid_permission_format("----------") is True

    def test_valid_directory_permissions(self):
        """Valid directory permission formats."""
        assert is_valid_permission_format("drwxr-xr-x") is True
        assert is_valid_permission_format("drwxrwxrwx") is True
        assert is_valid_permission_format("dr-xr-xr-x") is True

    def test_invalid_too_short(self):
        """Too short strings are invalid."""
        assert is_valid_permission_format("rw-r--r--") is False
        assert is_valid_permission_format("-rw") is False

    def test_invalid_too_long(self):
        """Too long strings are invalid."""
        assert is_valid_permission_format("-rw-r--r--x") is False

    def test_invalid_characters(self):
        """Invalid characters are rejected."""
        assert is_valid_permission_format("-rw-r--r-+") is False
        assert is_valid_permission_format("arw-r--r--") is False
        assert is_valid_permission_format("-rw-r--r-1") is False


class TestUpdateFileTimestamp:
    """Tests for update_file_timestamp function."""

    def test_update_with_unix_timestamps(self):
        """Update file with Unix timestamp strings."""
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"test")
            temp_path = f.name
        
        try:
            # Use Unix timestamps as strings
            ctime = "1705320600"
            mtime = "1705320700"
            
            update_file_timestamp(temp_path, ctime, mtime)
            
            stat = os.stat(temp_path)
            # Check that mtime was set
            assert abs(stat.st_mtime - 1705320700) < 1
        finally:
            os.unlink(temp_path)

    def test_update_with_iso_format(self):
        """Update file with ISO format strings."""
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"test")
            temp_path = f.name
        
        try:
            iso_time = "2024-01-15T12:30:00+00:00"
            
            update_file_timestamp(temp_path, ctime=iso_time, mtime=iso_time)
            
            expected_ts = datetime.fromisoformat(iso_time).timestamp()
            stat = os.stat(temp_path)
            assert abs(stat.st_mtime - expected_ts) < 1
        finally:
            os.unlink(temp_path)

    def test_update_only_mtime(self):
        """Update only modification time."""
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"test")
            temp_path = f.name
        
        try:
            mtime = "1705320700"
            
            update_file_timestamp(temp_path, mtime=mtime)
            
            stat = os.stat(temp_path)
            assert abs(stat.st_mtime - 1705320700) < 1
        finally:
            os.unlink(temp_path)

    def test_update_no_timestamps(self):
        """No update when both timestamps are None."""
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"test")
            temp_path = f.name
        
        try:
            original_mtime = os.stat(temp_path).st_mtime
            
            update_file_timestamp(temp_path, ctime=None, mtime=None)
            
            # Timestamp should be unchanged
            assert os.stat(temp_path).st_mtime == original_mtime
        finally:
            os.unlink(temp_path)

