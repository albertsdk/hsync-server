"""
Sync action model for HSYNC Server.

This module defines the SyncAction class and SyncActionName enum
for representing synchronization decisions (upload, download, or no action).
"""

from datetime import timedelta as td
from enum import Enum

from .file import File


class SyncActionName(str, Enum):
    """
    Enumeration of possible synchronization actions.

    These actions determine what operation to perform on a file
    during the synchronization process.
    """

    download = "download"  # File should be downloaded from server
    upload = "upload"  # File should be uploaded to server
    nothing = "nothing"  # No action needed, files are in sync


class SyncAction:
    """
    Represents a synchronization action for a file.

    This class encapsulates the decision about what action to take
    for a specific file during synchronization, along with the
    time difference between local and remote versions.

    Attributes:
        file: The File object this action applies to.
        name: The type of sync action (upload, download, or nothing).
        timedelta: Human-readable time difference between versions.
    """

    def __init__(
        self,
        file: File,
        name: SyncActionName | None = None,
        timedelta: int = 0,
    ):
        """
        Initialize a SyncAction instance.

        Args:
            file: The File object this action applies to.
            name: The type of synchronization action.
            timedelta: Time difference in seconds between versions.
        """
        self.file = file
        self.name = name
        self.timedelta = self._format_timedelta(timedelta)

    def __repr__(self) -> str:
        """Return string representation of the SyncAction instance."""
        return (
            f"{self.__class__.__name__}("
            f"file={self.file}, "
            f"name={self.name}, "
            f"timedelta={self.timedelta})"
        )

    def to_dict(self) -> dict:
        """
        Convert the SyncAction instance to a dictionary.

        Returns:
            Dictionary representation suitable for JSON serialization.
        """
        return {
            "file": self.file.to_dict(),
            "name": self.name,
            "timedelta": self.timedelta,
        }

    def _format_timedelta(self, delta_seconds: int) -> str:
        """
        Format a time delta in seconds to human-readable format.

        Converts seconds to a string like "2d 3h 15m 30s" showing
        days, hours, minutes, and seconds as appropriate.

        Args:
            delta_seconds: Time difference in seconds.

        Returns:
            Human-readable time difference string.
        """
        delta = td(seconds=delta_seconds)
        days = delta.days
        hours, remainder = divmod(delta.seconds, 3600)
        minutes, seconds = divmod(remainder, 60)

        parts = []
        if days > 0:
            parts.append(f"{days}d")
        if hours > 0:
            parts.append(f"{hours}h")
        if minutes > 0:
            parts.append(f"{minutes}m")
        if seconds > 0:
            parts.append(f"{seconds}s")

        return " ".join(parts) if parts else "0s"
