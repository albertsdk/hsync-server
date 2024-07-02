"""
Event model for HSYNC Server.

This module defines the Event class and EventName enum for representing
file system events (add, modify, rename, delete) that are broadcast
to connected clients via WebSocket.
"""

from enum import StrEnum
from os.path import join
from typing import TYPE_CHECKING, TypedDict

from .dir import Dir
from .file import File
from .symlink import Symlink

if TYPE_CHECKING:
    from .file import FileDict


# =============================================================================
# TypedDicts for type-safe serialization
# =============================================================================


class EventDict(TypedDict):
    """Type-safe dictionary representation of an Event."""

    cid: str
    name: str
    bucket: str
    src: "FileDict | dict"
    dst: "FileDict | dict | None"


class EventName(StrEnum):
    """
    Enumeration of supported file system event types.

    These events are used to notify clients about changes
    in the synchronized file system.
    """

    added = "added"  # File or directory was created
    modified = "modified"  # File or directory was modified
    renamed = "renamed"  # File or directory was renamed/moved
    deleted = "deleted"  # File or directory was deleted
    symlink = "symlink"  # Symbolic link was created or modified


class Event:
    """
    Represents a file system event for synchronization.

    Events are created when files or directories are added, modified,
    renamed, or deleted. They are broadcast to all connected WebSocket
    clients except the originator.

    Attributes:
        cid: Client ID of the event originator.
        bucket: Name of the bucket where the event occurred.
        name: Type of event (added, modified, renamed, deleted).
        src: Source file or directory affected by the event.
        dst: Destination file or directory (only for rename events).
    """

    def __init__(
        self,
        cid: str,
        localpath: str,
        bucket: str,
        name: EventName,
        src: File | Dir | Symlink,
        dst: File | Dir | Symlink | None = None,
        strip_localpath: bool = True,
    ):
        """
        Initialize an Event instance.

        Args:
            cid: Client ID of the event originator.
            localpath: Local base path for path normalization.
            bucket: Name of the bucket.
            name: Type of event.
            src: Source file, directory, or symlink.
            dst: Destination file, directory, or symlink (for rename events).
            strip_localpath: If True, remove localpath prefix from paths.
        """
        self.cid = cid
        self.bucket = bucket
        self.name = name.value

        def correct_path(
            entity: File | Dir | Symlink, localpath: str, strip_localpath: bool
        ) -> None:
            """
            Normalize the path in an entity based on localpath.

            Modifies the entity's path in place, either stripping the
            localpath prefix or prepending it.

            Args:
                entity: File, Dir, or Symlink instance to modify.
                localpath: Base path for normalization.
                strip_localpath: If True, remove prefix; if False, prepend it.
            """
            # Get the path based on entity type
            path = str(entity.path) if isinstance(entity, Dir) else str(entity.filepath)

            # Apply path transformation
            if strip_localpath:
                path = path.removeprefix(localpath).lstrip("/")
            else:
                path = join(localpath, path.lstrip("/"))

            # Assign the corrected path back to the entity
            if isinstance(entity, Dir):
                entity.path = path
            else:
                entity.filepath = path

        # Normalize paths for source and destination
        correct_path(src, localpath, strip_localpath)
        self.src = src

        if dst is not None:
            correct_path(dst, localpath, strip_localpath)
        self.dst = dst

    def __repr__(self) -> str:
        """Return string representation of the Event instance."""
        return (
            f"{self.__class__.__name__}("
            f"cid={self.cid}, "
            f"name={self.name}, "
            f"bucket={self.bucket}, "
            f"src={self.src}, "
            f"dst={self.dst})"
        )

    def to_dict(self) -> EventDict:
        """
        Convert the Event instance to a dictionary.

        Returns:
            EventDict representation suitable for JSON serialization.
        """
        return EventDict(
            cid=self.cid,
            name=self.name,
            bucket=self.bucket,
            src=self.src.to_dict(),
            dst=self.dst.to_dict() if self.dst else None,
        )
