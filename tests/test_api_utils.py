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
from hsync_server.api.utils import (
    get_permissions,
    set_permissions,
    get_dir_info,
    is_valid_bucket,
    is_valid_dir,
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


class TestGetPermissions:
    """Tests for get_permissions function."""

    @pytest.mark.asyncio
    async def test_get_file_permissions(self):
        """Get permissions of a file."""
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"test")
            temp_path = f.name

        try:
            os.chmod(temp_path, 0o644)
            permissions = await get_permissions(temp_path)
            assert permissions == "-rw-r--r--"
        finally:
            os.unlink(temp_path)

    @pytest.mark.asyncio
    async def test_get_directory_permissions(self, tmp_path):
        """Get permissions of a directory."""
        test_dir = tmp_path / "testdir"
        test_dir.mkdir()
        os.chmod(test_dir, 0o755)

        permissions = await get_permissions(str(test_dir))
        assert permissions == "drwxr-xr-x"

    @pytest.mark.asyncio
    async def test_get_executable_permissions(self):
        """Get permissions of an executable file."""
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"#!/bin/bash")
            temp_path = f.name

        try:
            os.chmod(temp_path, 0o755)
            permissions = await get_permissions(temp_path)
            assert permissions == "-rwxr-xr-x"
        finally:
            os.unlink(temp_path)


class TestSetPermissions:
    """Tests for set_permissions function."""

    @pytest.mark.asyncio
    async def test_set_file_permissions(self):
        """Set permissions on a file."""
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"test")
            temp_path = f.name

        try:
            await set_permissions(temp_path, "-rw-rw-r--")
            stat = os.stat(temp_path)
            assert stat.st_mode & 0o777 == 0o664
        finally:
            os.unlink(temp_path)

    @pytest.mark.asyncio
    async def test_set_executable_permissions(self):
        """Set executable permissions on a file."""
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"test")
            temp_path = f.name

        try:
            await set_permissions(temp_path, "-rwxr-xr-x")
            stat = os.stat(temp_path)
            assert stat.st_mode & 0o777 == 0o755
        finally:
            os.unlink(temp_path)


class TestGetDirInfo:
    """Tests for get_dir_info function."""

    @pytest.mark.asyncio
    async def test_get_dir_info(self, tmp_path):
        """Get directory info returns Dir object."""
        test_dir = tmp_path / "infodir"
        test_dir.mkdir()
        os.chmod(test_dir, 0o755)

        dir_info = await get_dir_info(str(test_dir))
        assert dir_info.path == str(test_dir)
        assert dir_info.permissions == "drwxr-xr-x"


class TestIsValidBucket:
    """Tests for is_valid_bucket function."""

    def test_valid_bucket_names(self):
        """Valid bucket names are accepted."""
        assert is_valid_bucket("mybucket") is True
        assert is_valid_bucket("my-bucket") is True
        assert is_valid_bucket("bucket123") is True

    def test_invalid_bucket_with_underscore(self):
        """Bucket names with underscores are rejected."""
        assert is_valid_bucket("my_bucket") is False

    def test_invalid_bucket_with_dots(self):
        """Bucket names with double dots are rejected."""
        assert is_valid_bucket("my..bucket") is False
        assert is_valid_bucket("../escape") is False

    def test_invalid_bucket_empty(self):
        """Empty bucket name is rejected."""
        assert is_valid_bucket("") is False


class TestIsValidDir:
    """Tests for is_valid_dir function."""

    def test_valid_paths(self):
        """Valid paths are accepted."""
        assert is_valid_dir("subdir") is True
        assert is_valid_dir("subdir/nested") is True
        assert is_valid_dir("") is True

    def test_path_traversal_rejected(self):
        """Path traversal attempts are rejected."""
        assert is_valid_dir("../escape") is False
        assert is_valid_dir("subdir/../../escape") is False

