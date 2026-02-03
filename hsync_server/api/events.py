# HSYNC Server - HTTP-based file synchronization server
# Copyright (C) 2025 Alberto Basso <alberto.sdk [at] gmail [dot] com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

"""
WebSocket events API for HSYNC Server.

This module provides real-time event synchronization via WebSocket.
Clients connect to receive file system events (add, modify, rename, delete)
and broadcast their own events to other connected clients.

The WebSocket endpoint handles:
- Client connection management
- Event validation and processing
- Broadcasting events to all connected clients (except sender)
- File system operations based on received events
"""

from hsync_server.api.utils import set_permissions
from hsync_server.config import HSYNC_API_KEY, HSYNC_BUCKETS
from hsync_server.hashes import del_md5_hash
from hsync_server.models.dir import Dir
from hsync_server.models.event import Event, EventName
from hsync_server.models.file import File
from hsync_server.models.symlink import Symlink
from hsync_server.routers import public_router
from fastapi import Query, WebSocket, WebSocketDisconnect
from json import loads, JSONDecodeError
from logging import getLogger
from os import makedirs, rename, remove, symlink as os_symlink, unlink
from os.path import dirname, exists, islink
from shutil import rmtree
from typing import Dict


# Logger instance for this module
LOG = getLogger("main")


class ConnectionManager:
    """
    Manages WebSocket connections for real-time event broadcasting.

    This class maintains a dictionary of active WebSocket connections,
    allowing events to be broadcast to all connected clients except
    the originator of the event.

    Attributes:
        active_connections: Dictionary mapping client IDs to WebSocket objects.
    """

    def __init__(self):
        """Initialize the ConnectionManager with empty connections dict."""
        self.active_connections: Dict[str, WebSocket] = {}

    async def connect(self, websocket: WebSocket, client_id: str) -> None:
        """
        Accept and register a new WebSocket connection.

        Args:
            websocket: The WebSocket connection to accept.
            client_id: Unique identifier for the connecting client.
        """
        await websocket.accept()
        self.active_connections[client_id] = websocket

    def disconnect(self, client_id: str) -> None:
        """
        Remove a client's WebSocket connection.

        Args:
            client_id: The client ID to disconnect.
        """
        self.active_connections.pop(client_id, None)

    async def send_message(self, event: Event) -> None:
        """
        Broadcast an event to all connected clients except the sender.

        Args:
            event: The Event object to broadcast.
        """
        for cid, connection in self.active_connections.items():
            # Don't send the event back to its originator
            if cid != event.cid:
                await connection.send_json(event.to_dict())


class BucketConnectionManager:
    """
    Manages per-bucket ConnectionManager instances for better scalability.

    Instead of a single global ConnectionManager, this class provides
    separate connection pools for each bucket. This improves:
    - Scalability: events only broadcast to relevant bucket subscribers
    - Isolation: bucket-specific issues don't affect other buckets
    - Performance: smaller connection pools for event broadcasting

    Attributes:
        buckets: Dictionary mapping bucket names to ConnectionManager instances.
    """

    def __init__(self):
        """Initialize with empty buckets dictionary."""
        self.buckets: Dict[str, ConnectionManager] = {}

    def get_manager(self, bucket: str) -> ConnectionManager:
        """
        Get or create a ConnectionManager for a specific bucket.

        Args:
            bucket: Name of the bucket.

        Returns:
            ConnectionManager instance for the specified bucket.
        """
        if bucket not in self.buckets:
            self.buckets[bucket] = ConnectionManager()
            LOG.debug(f"Created ConnectionManager for bucket: {bucket}")
        return self.buckets[bucket]

    def get_connection_count(self, bucket: str = None) -> int:
        """
        Get the number of active connections.

        Args:
            bucket: Optional bucket name. If None, returns total across all buckets.

        Returns:
            Number of active connections.
        """
        if bucket:
            manager = self.buckets.get(bucket)
            return len(manager.active_connections) if manager else 0
        return sum(
            len(m.active_connections) for m in self.buckets.values()
        )


# NOTE: WebSocket does not support Header Authentication
#       and FastAPI auto-docs for WebSocket endpoints
@public_router.websocket("/api/v1/buckets/{bucket}/events/{cid}")
async def ws_events(websocket: WebSocket, bucket: str, cid: str) -> None:
    """
    WebSocket endpoint for real-time file system event synchronization.

    This endpoint allows clients to:
    - Receive events from other clients in real-time
    - Send their own file system events to be broadcast
    - Trigger server-side file operations based on events

    Authentication is done via the first message after connection:
    Client must send: {"type": "auth", "token": "<api_key>"}
    Server responds: {"type": "auth", "status": "ok"}

    Args:
        websocket: The WebSocket connection object.
        bucket: Name of the bucket to sync events for.
        cid: Unique client ID for the connecting client.

    Raises:
        ValueError: If the API key is invalid.
    """
    try:
        # Accept the connection first (required before receiving messages)
        await websocket.accept()

        # Wait for authentication message
        try:
            auth_data = await websocket.receive_text()
            auth_message = loads(auth_data)

            if auth_message.get("type") != "auth":
                LOG.warning(f"[WS] Invalid auth message type from {cid}")
                await websocket.close(code=4001, reason="Invalid auth message")
                return

            auth_token = auth_message.get("token")
            if auth_token != HSYNC_API_KEY:
                LOG.warning(f"[WS] Invalid API key from {cid}")
                await websocket.close(code=4003, reason="Invalid API key")
                return

            LOG.debug(f"[WS] Client {cid} authenticated")
            # Send auth confirmation
            await websocket.send_json({"type": "auth", "status": "ok"})

        except Exception as e:
            LOG.warning(f"[WS] Auth failed for {cid}: {e}")
            await websocket.close(code=4003, reason="Authentication failed")
            return

        # Register the authenticated client with bucket-specific manager
        manager = bucket_manager.get_manager(bucket)
        manager.active_connections[cid] = websocket
        LOG.info(f"[WS] Client connected: {cid} (bucket: {bucket})")
        try:
            # Main message processing loop
            while True:
                data = await websocket.receive_text()
                LOG.debug(f"[WS] Received message: {data}")
                try:
                    message = loads(data)

                    # Validate event type
                    if message.get("name") not in {
                        EventName.added.value,
                        EventName.modified.value,
                        EventName.renamed.value,
                        EventName.deleted.value,
                        EventName.symlink.value,
                    }:
                        LOG.error(f"Invalid event name: {message.get('name')}")
                        continue

                    # Extract event data
                    src = message.get("src", None)
                    dst = message.get("dst", None)
                    bucket_dir = f"{HSYNC_BUCKETS}/{bucket}"
                    event_name = EventName(message.get("name"))

                    # Initialize event to None - will be set if valid event processed
                    event = None

                    # Handle "added" events - create new files/directories
                    if event_name == EventName.added:
                        # Handle directory creation
                        if src and "path" in src:
                            dir = Dir().from_dict(src, bucket_dir)
                            if dir.path and dir.permissions:
                                LOG.info(f'[WS] Added dir "{dir.path}"')
                                makedirs(dir.path, exist_ok=True)
                                await set_permissions(dir.path, dir.permissions)
                                event = Event(
                                    cid, bucket_dir, bucket, EventName.added, dir
                                )
                        # Handle file creation notification
                        elif src and "filepath" in src:
                            file = File().from_dict(src, bucket_dir)
                            LOG.info(f'[WS] Added file "{file.filepath}"')
                            event = Event(
                                cid, bucket_dir, bucket, EventName.added, file
                            )

                    # Handle "modified" events - update existing files/directories
                    elif event_name == EventName.modified:
                        # Handle directory modification (permissions)
                        if src and "path" in src:
                            dir = Dir().from_dict(src, bucket_dir)
                            if dir.path and dir.permissions:
                                LOG.info(f'[WS] Modified dir "{dir.path}"')
                                await set_permissions(dir.path, dir.permissions)
                                event = Event(
                                    cid, bucket_dir, bucket, EventName.modified, dir
                                )
                        # Handle file modification notification
                        elif src and "filepath" in src:
                            file = File().from_dict(src, bucket_dir)
                            LOG.info(f'[WS] Modified file "{file.filepath}"')
                            event = Event(
                                cid, bucket_dir, bucket, EventName.modified, file
                            )
                    # Handle "deleted" events - remove files/directories
                    elif event_name == EventName.deleted:
                        # Handle directory deletion
                        if src and "path" in src:
                            dir = Dir().from_dict(src, bucket_dir)
                            if dir.path:
                                if not exists(dir.path):
                                    LOG.warning(f'[WS] Dir: "{dir.path}" not found.')
                                else:
                                    LOG.info(f'[WS] Deleted dir "{dir.path}"')
                                    rmtree(dir.path, ignore_errors=False)
                                event = Event(
                                    cid, bucket_dir, bucket, EventName.deleted, dir
                                )
                        # Handle file deletion
                        elif src and "filepath" in src:
                            file = File().from_dict(src, bucket_dir, False)
                            if file.filepath:
                                if not exists(file.filepath):
                                    LOG.warning(
                                        f'[WS] File: "{file.filepath}" not found.'
                                    )
                                else:
                                    LOG.info(f'[WS] Deleted file "{file.filepath}"')
                                    remove(file.filepath)
                                # Remove from hash cache
                                del_md5_hash(file.filepath)
                                event = Event(
                                    cid, bucket_dir, bucket, EventName.deleted, file
                                )

                    # Handle "renamed" events - rename/move files or directories
                    elif event_name == EventName.renamed:
                        # Handle directory rename
                        if src and src.get("path", None) and dst:
                            src_dir = Dir().from_dict(src, bucket_dir)
                            dst_dir = Dir().from_dict(dst, bucket_dir)
                            if src_dir.path and dst_dir.path:
                                LOG.info(
                                    f'[WS] Renamed "{src_dir.path}" -> "{dst_dir.path}"'
                                )
                                rename(src_dir.path, dst_dir.path)
                                event = Event(
                                    cid,
                                    bucket_dir,
                                    bucket,
                                    EventName.renamed,
                                    src_dir,
                                    dst_dir,
                                )
                        # Handle file rename
                        elif src and src.get("filepath", None) and dst:
                            src_file = File().from_dict(src, bucket_dir, False)
                            dst_file = File().from_dict(dst, bucket_dir, False)
                            if src_file.filepath and dst_file.filepath:
                                LOG.info(
                                    f'[WS] Renamed "{src_file.filepath}" -> "{dst_file.filepath}"'
                                )
                                rename(src_file.filepath, dst_file.filepath)
                                event = Event(
                                    cid,
                                    bucket_dir,
                                    bucket,
                                    EventName.renamed,
                                    src_file,
                                    dst_file,
                                )

                    # Handle "symlink" events - create or update symbolic links
                    elif event_name == EventName.symlink:
                        if src and "type" in src and src.get("type") == "symlink":
                            link = Symlink.from_dict(src)
                            # Build the full path
                            link.filepath = f"{bucket_dir}/{link.filepath.lstrip('/')}"
                            target = link.target

                            if link.filepath and target:
                                LOG.info(
                                    f'[WS] Symlink "{link.filepath}" -> "{target}"'
                                )
                                # Ensure parent directory exists
                                parent_dir = dirname(link.filepath)
                                if not exists(parent_dir):
                                    makedirs(parent_dir, exist_ok=True)

                                # Remove existing symlink or file if present
                                if islink(link.filepath) or exists(link.filepath):
                                    unlink(link.filepath)

                                # Create the symlink
                                os_symlink(target, link.filepath)
                                event = Event(
                                    cid, bucket_dir, bucket, EventName.symlink, link
                                )

                    # Broadcast the event to all other connected clients
                    if event:
                        await manager.send_message(event)

                except JSONDecodeError:
                    LOG.error(f"[WS] Invalid JSON received: {data}")
                except Exception as e:
                    LOG.error(f"[WS] Exception data: {data}")
                    LOG.error(f"[WS] Exception: {e}", exc_info=True)

        except WebSocketDisconnect:
            # Client disconnected normally
            manager.disconnect(cid)
            LOG.warning(f"[WS] Disconnected (CID: {cid})")
        except Exception as e:
            # Client disconnected with error
            manager.disconnect(cid)
            LOG.warning(f"[WS] Disconnected (CID: {cid}) ({e})")
            await websocket.close()
    except Exception:
        # Handle any outer exception (e.g., auth failure)
        LOG.warning(f"[WS] Disconnected (CID: {cid})")
        await websocket.close()


# Global bucket connection manager instance for WebSocket connections
# Provides per-bucket connection management for better scalability
bucket_manager = BucketConnectionManager()


LOG.info(f"Module {__name__} loaded")
