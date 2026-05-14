"""
File model for HSYNC Server.

This module defines the File class representing a file with its metadata
(path, timestamps, size, checksum, permissions) used for file synchronization.
"""

import stat
from datetime import UTC, datetime
from hashlib import md5
from os import stat as os_stat
from os.path import (
    basename,
    exists,
    getctime,
    getmtime,
    getsize,
    join,
)
from re import match
from typing import Optional, TypedDict

# =============================================================================
# MD5 Cache (mirrors hsync-client implementation)
# =============================================================================

# In-memory cache for MD5 checksums to avoid redundant computation
# during a single sync operation. Key: filepath, Value: tuple[md5sum, mtime]
_md5_cache: dict[str, tuple[str, int]] = {}

# Chunk sizes based on file size for optimal transfer
_CHUNK_SIZE_SMALL = 8 * 1024  # 8KB for files < 1MB
_CHUNK_SIZE_MEDIUM = 64 * 1024  # 64KB for files < 100MB
_CHUNK_SIZE_LARGE = 256 * 1024  # 256KB for files >= 100MB
_FILE_SIZE_SMALL = 1_000_000  # 1MB
_FILE_SIZE_MEDIUM = 100_000_000  # 100MB


def clear_md5_cache() -> None:
    """Clear the entire MD5 cache.

    Should be called at the start of each sync cycle to ensure
    fresh checksums are computed for all files.
    """
    _md5_cache.clear()


def invalidate_md5_cache(file_path: str) -> None:
    """Remove a single entry from the MD5 cache.

    Should be called when a file is known to have been modified
    (e.g. after an upload) so that the next ``File.get()``
    recomputes the checksum.

    Args:
        file_path: Path of the file to invalidate.
    """
    _md5_cache.pop(file_path, None)


def _get_cached_md5(file_path: str, current_mtime: int) -> str | None:
    """Get cached MD5 if file hasn't been modified."""
    if file_path in _md5_cache:
        cached_md5, cached_mtime = _md5_cache[file_path]
        if cached_mtime == current_mtime:
            return cached_md5
        del _md5_cache[file_path]
    return None


def _set_cached_md5(file_path: str, md5sum: str, mtime: int) -> None:
    """Store MD5 in cache with its mtime for invalidation."""
    _md5_cache[file_path] = (md5sum, mtime)


# =============================================================================
# TypedDicts for type-safe serialization
# =============================================================================


class FileDict(TypedDict):
    """Type-safe dictionary representation of a File."""

    filepath: str | None
    name: str | None
    ctime: int | None
    mtime: int | None
    size: int
    md5sum: str | None
    permissions: str | None


def is_hidden_file(file_path: str) -> bool:
    """
    Check if a file should be considered hidden or temporary.

    Hidden files include:
    - Files starting with '.' (dotfiles)
    - Backup files ending with '~' or '.bak'
    - Temporary files ending with '.tmp' or '.temp'
    - Editor swap files (.swp, .swo, .swx)
    - Lock files (.~lock.*)
    - Vim test file (4913)
    - Timestamped backup files (-YYYY-MM-DD-HHmmss)

    Args:
        file_path: Path to the file to check.

    Returns:
        True if the file is hidden/temporary, False otherwise.
    """
    from hsync_server.constants import HSYNCKEEP_FILENAME

    name = basename(file_path)

    # Sentinel file for empty-directory sync — never hidden
    if name == HSYNCKEEP_FILENAME:
        return False

    hidden_file_regex = (
        r"(^\..*|.*~$|^4913$|^\..*\.sw[px]$|^\.~lock\..*#$|"
        r".*\.bak$|.*\.tmp$|.*\.temp$|.*\.swp$|.*\.swo$|"
        r".*-\d{4}-\d{2}-\d{2}-\d{6}$)"
    )
    return bool(match(hidden_file_regex, name))


class File:
    """
    Represents a file with its metadata for synchronization.

    This class stores all relevant file information needed for
    synchronization decisions, including timestamps, size, checksum,
    and permissions.

    Attributes:
        filepath: Full path to the file.
        name: Base name of the file.
        ctime: Creation time as Unix timestamp.
        mtime: Modification time as Unix timestamp.
        size: File size in bytes.
        md5sum: MD5 checksum of file content.
        permissions: Symbolic permission string.
    """

    def __init__(
        self,
        filepath: str | None = None,
        ctime: str | int | float | datetime | None = None,
        mtime: str | int | float | datetime | None = None,
        size: int = 0,
        md5sum: str | None = None,
        permissions: str | None = None,
    ):
        """
        Initialize a File instance.

        Args:
            filepath: Full path to the file.
            ctime: Creation time (various formats accepted).
            mtime: Modification time (various formats accepted).
            size: File size in bytes.
            md5sum: MD5 checksum of file content.
            permissions: Symbolic permission string.
        """
        self.filepath = filepath
        self.name = basename(filepath) if filepath else None
        self.size = size
        self.md5sum = md5sum
        self.permissions = permissions
        self.ctime = self._parse_time(ctime)
        self.mtime = self._parse_time(mtime)

    def __repr__(self) -> str:
        """Return string representation of the File instance."""
        return (
            f"{self.__class__.__name__}("
            f"filepath={self.filepath}, "
            f"name={self.name}, "
            f"ctime={self.ctime}, "
            f"mtime={self.mtime}, "
            f"size={self.size}, "
            f"md5sum={self.md5sum}, "
            f"permissions={self.permissions})"
        )

    def _parse_time(self, time: str | int | float | datetime | None) -> int | None:
        """
        Parse various time formats to Unix timestamp.

        Accepts datetime objects, ISO format strings, or numeric timestamps.

        Args:
            time: Time value in various formats.

        Returns:
            Unix timestamp as integer, or None if input is None.
        """
        if isinstance(time, datetime):
            return int(time.timestamp())
        elif isinstance(time, str):
            return int(datetime.fromisoformat(time).timestamp())
        elif isinstance(time, int) or isinstance(time, float):
            return int(datetime.fromtimestamp(time, UTC).timestamp())
        return None

    @staticmethod
    def _compute_md5(filepath: str, file_size: int | None = None) -> str:
        """
        Compute the MD5 checksum of a file.

        Reads the file in chunks to avoid loading large files entirely
        into memory. Uses adaptive chunk size based on file size for
        optimal performance.

        Args:
            filepath: Path to the file.
            file_size: Size of the file in bytes. If not provided, will
                be determined from filesystem.

        Returns:
            Hexadecimal MD5 digest string.
        """
        # Determine optimal chunk size based on file size
        if file_size is not None:
            if file_size >= _FILE_SIZE_MEDIUM:
                chunk_size = _CHUNK_SIZE_LARGE
            elif file_size >= _FILE_SIZE_SMALL:
                chunk_size = _CHUNK_SIZE_MEDIUM
            else:
                chunk_size = _CHUNK_SIZE_SMALL
        else:
            chunk_size = _CHUNK_SIZE_SMALL

        hash_md5 = md5()
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(chunk_size), b""):
                hash_md5.update(chunk)
        return hash_md5.hexdigest()

    @staticmethod
    def _get_permissions(obj_path: str) -> str:
        """
        Get file permissions in symbolic notation.

        Args:
            obj_path: Path to the file.

        Returns:
            Permission string in symbolic format.
        """
        obj_stat = os_stat(obj_path)
        permissions = obj_stat.st_mode
        return stat.filemode(permissions)

    def to_dict(self) -> FileDict:
        """
        Convert the File instance to a dictionary.

        Returns:
            FileDict representation suitable for JSON serialization.
        """
        return FileDict(
            filepath=self.filepath,
            name=self.name,
            ctime=self.ctime,
            mtime=self.mtime,
            size=self.size,
            md5sum=self.md5sum,
            permissions=self.permissions,
        )

    @classmethod
    def get(cls, file_path: str, buckets_dir: str | None = None) -> Optional["File"]:
        """
        Get a File instance for an existing file.

        Reads all metadata from the file system including timestamps,
        size, checksum, and permissions.

        Args:
            file_path: Path to the file.
            buckets_dir: Optional base directory to prepend.

        Returns:
            File instance if file exists, None otherwise.
        """
        file_path = join(buckets_dir, file_path.lstrip("/")) if buckets_dir else file_path
        if exists(file_path):
            file_size = getsize(file_path)
            file_mtime = int(getmtime(file_path))

            # Try to get cached MD5 (with mtime validation)
            md5sum = _get_cached_md5(file_path, file_mtime)
            if md5sum is None:
                md5sum = cls._compute_md5(file_path, file_size)
                _set_cached_md5(file_path, md5sum, file_mtime)

            return cls(
                filepath=file_path,
                ctime=int(getctime(file_path)),
                mtime=file_mtime,
                size=file_size,
                md5sum=md5sum,
                permissions=cls._get_permissions(file_path),
            )
        else:
            return None

    @classmethod
    def from_dict(
        cls,
        data: dict | FileDict,
        dir_prefix: str | None = None,
        strip_dir_prefix: bool = True,
    ) -> "File":
        """
        Create a File instance from a dictionary.

        Args:
            data: Dictionary containing file metadata.
            dir_prefix: Base path for path manipulation.
            strip_dir_prefix: If True, remove prefix; if False, prepend it.

        Returns:
            New File instance.

        Raises:
            ValueError: If strip_dir_prefix is True but dir_prefix is not set.
        """
        if strip_dir_prefix and not dir_prefix:
            raise ValueError("Set dir_prefix to strip it.")

        data_filepath = data.get("filepath", "")
        filepath = (
            str(data_filepath).removeprefix(str(dir_prefix)).lstrip("/")
            if strip_dir_prefix
            else join(str(dir_prefix), str(data_filepath).lstrip("/"))
        )

        return cls(
            filepath=filepath,
            ctime=data.get("ctime"),
            mtime=data.get("mtime"),
            size=data.get("size", 0),
            md5sum=data.get("md5sum"),
            permissions=data.get("permissions"),
        )
