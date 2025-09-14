"""
Tests for API utility functions.
"""

import os
from datetime import datetime
from unittest.mock import patch

import pytest
from hsync_common.permissions import is_valid_octal_permissions, octal_to_mode

from hsync_server.api.file_utils import (
    update_file_timestamp,
)
from hsync_server.api.utils import (
    get_dir_info,
    get_permissions,
    is_valid_bucket,
    resolve_within_bucket,
    set_directory_permissions,
    set_permissions,
)


class TestIsValidPermissionFormat:
    """Tests for is_valid_octal_permissions function."""

    def test_valid_file_permissions(self):
        """Valid octal permission formats."""
        assert is_valid_octal_permissions("644") is True
        assert is_valid_octal_permissions("755") is True
        assert is_valid_octal_permissions("777") is True
        assert is_valid_octal_permissions("000") is True

    def test_valid_special_bits(self):
        """Four-digit octal strings carry the special bits."""
        assert is_valid_octal_permissions("4755") is True
        assert is_valid_octal_permissions("0644") is True
        assert is_valid_octal_permissions("1777") is True

    def test_invalid_too_short(self):
        """Too short strings are invalid."""
        assert is_valid_octal_permissions("64") is False
        assert is_valid_octal_permissions("") is False

    def test_invalid_too_long(self):
        """Too long strings are invalid."""
        assert is_valid_octal_permissions("06440") is False

    def test_invalid_characters(self):
        """Invalid characters are rejected."""
        assert is_valid_octal_permissions("-rw-r--r--") is False
        assert is_valid_octal_permissions("648") is False
        assert is_valid_octal_permissions("64x") is False


class TestUpdateFileTimestamp:
    """Tests for update_file_timestamp function."""

    def test_update_with_unix_timestamps(self, tmp_path):
        """Update file with Unix timestamp strings."""
        temp_path = tmp_path / "test.txt"
        temp_path.write_bytes(b"test")

        # Use Unix timestamps as strings (ctime differs from mtime on purpose)
        ctime = "1705320600"
        mtime = "1705320700"

        update_file_timestamp(str(temp_path), ctime, mtime)

        stat = os.stat(str(temp_path))
        # mtime is restored from the mtime argument
        assert abs(stat.st_mtime - 1705320700) < 1
        # atime mirrors mtime; ctime must NOT be written into atime
        assert abs(stat.st_atime - 1705320700) < 1

    def test_update_with_iso_format(self, tmp_path):
        """Update file with ISO format strings."""
        temp_path = tmp_path / "test.txt"
        temp_path.write_bytes(b"test")

        iso_time = "2024-01-15T12:30:00+00:00"

        update_file_timestamp(str(temp_path), ctime=iso_time, mtime=iso_time)

        expected_ts = datetime.fromisoformat(iso_time).timestamp()
        stat = os.stat(str(temp_path))
        assert abs(stat.st_mtime - expected_ts) < 1

    def test_update_only_mtime(self, tmp_path):
        """Update only modification time."""
        temp_path = tmp_path / "test.txt"
        temp_path.write_bytes(b"test")

        mtime = "1705320700"

        update_file_timestamp(str(temp_path), mtime=mtime)

        stat = os.stat(str(temp_path))
        assert abs(stat.st_mtime - 1705320700) < 1

    def test_update_no_timestamps(self, tmp_path):
        """No update when both timestamps are None."""
        temp_path = tmp_path / "test.txt"
        temp_path.write_bytes(b"test")

        original_mtime = os.stat(str(temp_path)).st_mtime

        update_file_timestamp(str(temp_path), ctime=None, mtime=None)

        # Timestamp should be unchanged
        assert os.stat(str(temp_path)).st_mtime == original_mtime


class TestGetPermissions:
    """Tests for get_permissions function."""

    @pytest.mark.asyncio
    async def test_get_file_permissions(self, tmp_path):
        """Get permissions of a file."""
        temp_path = tmp_path / "test.txt"
        temp_path.write_bytes(b"test")

        os.chmod(str(temp_path), 0o644)
        permissions = await get_permissions(str(temp_path))
        assert permissions == "644"

    @pytest.mark.asyncio
    async def test_get_directory_permissions(self, tmp_path):
        """Get permissions of a directory."""
        test_dir = tmp_path / "testdir"
        test_dir.mkdir()
        os.chmod(test_dir, 0o755)

        permissions = await get_permissions(str(test_dir))
        assert permissions == "755"

    @pytest.mark.asyncio
    async def test_get_executable_permissions(self, tmp_path):
        """Get permissions of an executable file."""
        temp_path = tmp_path / "test.sh"
        temp_path.write_text("#!/bin/bash")

        os.chmod(str(temp_path), 0o755)
        permissions = await get_permissions(str(temp_path))
        assert permissions == "755"


class TestSetPermissions:
    """Tests for set_permissions function."""

    def test_octal_to_mode(self):
        """Octal permission strings are converted to numeric mode."""
        assert octal_to_mode("664") == 0o664
        assert octal_to_mode("755") == 0o755

    def test_octal_to_mode_special_bits(self):
        """Optional leading digit and special bits are preserved."""
        assert octal_to_mode("0644") == 0o644
        assert octal_to_mode("4755") == 0o4755

    @pytest.mark.asyncio
    async def test_set_file_permissions(self, tmp_path):
        """Set permissions on a file."""
        temp_path = tmp_path / "test.txt"
        temp_path.write_bytes(b"test")

        await set_permissions(str(temp_path), "664")
        stat = os.stat(str(temp_path))
        assert stat.st_mode & 0o777 == 0o664

    @pytest.mark.asyncio
    async def test_set_executable_permissions(self, tmp_path):
        """Set executable permissions on a file."""
        temp_path = tmp_path / "test.txt"
        temp_path.write_bytes(b"test")

        await set_permissions(str(temp_path), "755")
        stat = os.stat(str(temp_path))
        assert stat.st_mode & 0o777 == 0o755

    @pytest.mark.asyncio
    async def test_set_numeric_permissions(self, tmp_path):
        """Numeric permissions do not collapse to chmod 000."""
        temp_path = tmp_path / "test.txt"
        temp_path.write_bytes(b"test")

        await set_permissions(str(temp_path), "644")
        stat = os.stat(str(temp_path))
        assert stat.st_mode & 0o777 == 0o644

    @pytest.mark.asyncio
    async def test_set_directory_permissions_keeps_owner_access(self, tmp_path):
        """Bucket directories must remain accessible to the server owner."""
        test_dir = tmp_path / "restricted"
        test_dir.mkdir()

        await set_directory_permissions(str(test_dir), "000")

        stat = os.stat(test_dir)
        assert stat.st_mode & 0o700 == 0o700


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
        assert dir_info.permissions == "755"


class TestIsValidBucket:
    """Tests for is_valid_bucket function."""

    def test_valid_bucket_names(self):
        """Valid bucket names are accepted."""
        assert is_valid_bucket("mybucket") is True
        assert is_valid_bucket("my-bucket") is True
        assert is_valid_bucket("my_bucket") is True
        assert is_valid_bucket("bucket123") is True
        assert is_valid_bucket("my-bucket_123") is True
        assert is_valid_bucket("a") is True
        assert is_valid_bucket("a1") is True

    def test_invalid_bucket_start_end(self):
        """Bucket names starting/ending with hyphen or underscore are rejected."""
        assert is_valid_bucket("-bucket") is False
        assert is_valid_bucket("bucket-") is False
        assert is_valid_bucket("_bucket") is False
        assert is_valid_bucket("bucket_") is False

    def test_invalid_bucket_with_dots(self):
        """Bucket names with dots are rejected."""
        assert is_valid_bucket("my..bucket") is False
        assert is_valid_bucket("../escape") is False
        assert is_valid_bucket("my.bucket") is False

    def test_invalid_bucket_empty(self):
        """Empty bucket name is rejected."""
        assert is_valid_bucket("") is False

    def test_invalid_bucket_special_chars(self):
        """Bucket names with special characters are rejected."""
        assert is_valid_bucket("my@bucket") is False
        assert is_valid_bucket("my bucket") is False
        assert is_valid_bucket("my/bucket") is False


class TestResolveWithinBucket:
    """Tests for resolve_within_bucket (per-bucket confinement)."""

    def test_valid_paths(self, tmp_path):
        """Valid relative paths resolve inside the bucket."""
        with patch("hsync_server.api.utils.get_buckets_path", return_value=str(tmp_path)):
            assert resolve_within_bucket("b", "subdir") == str(
                (tmp_path / "b" / "subdir").resolve()
            )
            assert resolve_within_bucket("b", "subdir/nested") == str(
                (tmp_path / "b" / "subdir" / "nested").resolve()
            )
            # Empty and "." refer to the bucket root
            assert resolve_within_bucket("b", "") == str((tmp_path / "b").resolve())
            assert resolve_within_bucket("b", ".") == str((tmp_path / "b").resolve())

    def test_path_traversal_rejected(self, tmp_path):
        """Path traversal attempts are rejected."""
        with patch("hsync_server.api.utils.get_buckets_path", return_value=str(tmp_path)):
            assert resolve_within_bucket("b", "../escape") is None
            assert resolve_within_bucket("b", "subdir/../../escape") is None

    def test_cross_bucket_rejected(self, tmp_path):
        """A path escaping into a sibling bucket is rejected."""
        with patch("hsync_server.api.utils.get_buckets_path", return_value=str(tmp_path)):
            assert resolve_within_bucket("A", "../B/x") is None

    def test_backslash_and_absolute_rejected(self, tmp_path):
        """Windows separators and absolute paths are rejected."""
        with patch("hsync_server.api.utils.get_buckets_path", return_value=str(tmp_path)):
            assert resolve_within_bucket("b", "..\\..\\evil") is None
            assert resolve_within_bucket("b", "/etc/passwd") is None

    def test_internal_names_rejected(self, tmp_path):
        """Engine-owned files are rejected."""
        with patch("hsync_server.api.utils.get_buckets_path", return_value=str(tmp_path)):
            assert resolve_within_bucket("b", ".hsyncignore") is None
            assert resolve_within_bucket("b", "sub/.hsyncstate") is None


# =============================================================================
# Path Validation Tests
# =============================================================================


from hsync_common.path_validation import (
    FORBIDDEN_FILENAME_CHARS,
    has_control_chars,
    has_forbidden_chars,
    has_problematic_trailing,
    is_reserved_name,
    is_safe_path_component,
    is_valid_filename,
    sanitize_filename,
)


class TestIsValidFilename:
    """Tests for is_valid_filename function."""

    def test_valid_filenames(self):
        """Valid filenames are accepted."""
        assert is_valid_filename("file.txt") is True
        assert is_valid_filename("document.pdf") is True
        assert is_valid_filename("image-2024.png") is True
        assert is_valid_filename("data_file.csv") is True

    def test_empty_filename_rejected(self):
        """Empty filename is rejected."""
        assert is_valid_filename("") is False

    def test_forbidden_chars_rejected(self):
        """Filenames with forbidden characters are rejected."""
        assert is_valid_filename("file<name>.txt") is False
        assert is_valid_filename("file:name.txt") is False
        assert is_valid_filename('file"name.txt') is False
        assert is_valid_filename("file|name.txt") is False
        assert is_valid_filename("file?name.txt") is False
        assert is_valid_filename("file*name.txt") is False

    def test_control_chars_rejected(self):
        """Filenames with control characters are rejected."""
        assert is_valid_filename("file\x01name.txt") is False
        assert is_valid_filename("file\x1fname.txt") is False

    def test_nul_char_rejected(self):
        """Filenames with NUL character are rejected."""
        assert is_valid_filename("file\x00name.txt") is False

    def test_reserved_names_rejected(self):
        """Windows reserved names are rejected."""
        assert is_valid_filename("CON") is False
        assert is_valid_filename("PRN") is False
        assert is_valid_filename("AUX") is False
        assert is_valid_filename("NUL") is False
        assert is_valid_filename("COM1") is False
        assert is_valid_filename("LPT1") is False

    def test_reserved_names_case_insensitive(self):
        """Reserved names are rejected regardless of case."""
        assert is_valid_filename("con") is False
        assert is_valid_filename("Con") is False
        assert is_valid_filename("nul") is False

    def test_reserved_names_with_extension_rejected(self):
        """Reserved names with extensions are rejected."""
        assert is_valid_filename("NUL.txt") is False
        assert is_valid_filename("COM1.log") is False
        assert is_valid_filename("con.exe") is False

    def test_reserved_names_all_ports(self):
        """All COM and LPT port numbers are rejected."""
        for i in range(1, 10):
            assert is_valid_filename(f"COM{i}") is False
            assert is_valid_filename(f"LPT{i}") is False

    def test_reserved_names_superscript(self):
        """Superscript digit variants of COM and LPT are rejected."""
        assert is_valid_filename("COM\u00b9") is False  # COM¹
        assert is_valid_filename("COM\u00b2") is False  # COM²
        assert is_valid_filename("COM\u00b3") is False  # COM³
        assert is_valid_filename("LPT\u00b9") is False  # LPT¹

    def test_trailing_space_rejected(self):
        """Filenames with trailing space are rejected."""
        assert is_valid_filename("file.txt ") is False
        assert is_valid_filename("name ") is False

    def test_trailing_period_rejected(self):
        """Filenames with trailing period are rejected."""
        assert is_valid_filename("file.") is False
        assert is_valid_filename("name.") is False


class TestHasForbiddenChars:
    """Tests for has_forbidden_chars function."""

    def test_no_forbidden_chars(self):
        """Returns False when no forbidden chars."""
        assert has_forbidden_chars("valid_name.txt", FORBIDDEN_FILENAME_CHARS) is False

    def test_has_forbidden_chars(self):
        """Returns True when forbidden chars present."""
        assert has_forbidden_chars("file<name", FORBIDDEN_FILENAME_CHARS) is True
        assert has_forbidden_chars("file>name", FORBIDDEN_FILENAME_CHARS) is True


class TestHasControlChars:
    """Tests for has_control_chars function."""

    def test_no_control_chars(self):
        """Returns False when no control chars."""
        assert has_control_chars("valid_name.txt") is False

    def test_has_control_chars(self):
        """Returns True when control chars present."""
        assert has_control_chars("file\x01name") is True
        assert has_control_chars("file\x1fname") is True


class TestIsReservedName:
    """Tests for is_reserved_name function."""

    def test_not_reserved(self):
        """Returns False for normal names."""
        assert is_reserved_name("document.txt") is False
        assert is_reserved_name("console.log") is False  # 'console' is not reserved

    def test_reserved(self):
        """Returns True for reserved names."""
        assert is_reserved_name("CON") is True
        assert is_reserved_name("NUL.txt") is True


class TestHasProblematicTrailing:
    """Tests for has_problematic_trailing function."""

    def test_no_problematic_trailing(self):
        """Returns False when no problematic trailing."""
        assert has_problematic_trailing("file.txt") is False

    def test_trailing_space(self):
        """Returns True for trailing space."""
        assert has_problematic_trailing("file ") is True

    def test_trailing_period(self):
        """Returns True for trailing period."""
        assert has_problematic_trailing("file.") is True


class TestIsSafePathComponent:
    """Tests for is_safe_path_component function."""

    def test_valid_paths(self):
        """Valid path components are accepted."""
        assert is_safe_path_component("file.txt") is True
        assert is_safe_path_component("subdir") is True
        assert is_safe_path_component("path/to/file") is True

    def test_absolute_path_rejected(self):
        """Absolute paths are rejected."""
        assert is_safe_path_component("/etc/passwd") is False
        assert is_safe_path_component("/absolute/path") is False

    def test_windows_absolute_rejected(self):
        """Windows absolute paths are rejected."""
        assert is_safe_path_component("C:\\windows") is False
        assert is_safe_path_component("D:\\data") is False

    def test_traversal_rejected(self):
        """Path traversal attempts are rejected."""
        assert is_safe_path_component("../etc/passwd") is False
        assert is_safe_path_component("..") is False
        assert is_safe_path_component("../../etc") is False
        assert is_safe_path_component("dir/../../../etc") is False


class TestSanitizeFilename:
    """Tests for sanitize_filename function."""

    def test_normal_filename_unchanged(self):
        """Normal filenames are kept as-is."""
        assert sanitize_filename("file.txt") == "file.txt"
        assert sanitize_filename("document.pdf") == "document.pdf"

    def test_path_removed(self):
        """Path separators are removed, keeping only filename."""
        assert sanitize_filename("path/to/file.txt") == "file.txt"
        assert sanitize_filename("C:\\Windows\\file.txt") == "file.txt"

    def test_forbidden_chars_replaced(self):
        """Forbidden characters are replaced with underscores."""
        assert sanitize_filename("file<name>.txt") == "file_name_.txt"
        assert sanitize_filename("file:name.txt") == "file_name.txt"

    def test_trailing_removed(self):
        """Trailing spaces and periods are removed."""
        assert sanitize_filename("file.txt ") == "file.txt"
        assert sanitize_filename("file.txt.") == "file.txt"

    def test_empty_becomes_unnamed(self):
        """Empty strings and reserved names become 'unnamed'."""
        assert sanitize_filename("") == "unnamed"
        # Reserved names are also sanitized to 'unnamed'
        assert sanitize_filename("NUL") == "unnamed"
        assert sanitize_filename("CON") == "unnamed"
        assert sanitize_filename("   ") == "unnamed"

    def test_long_filename_truncated(self):
        """Filenames longer than 255 chars are truncated."""
        long_name = "a" * 300 + ".txt"
        result = sanitize_filename(long_name)
        assert len(result) <= 255
        assert result.endswith(".txt")
