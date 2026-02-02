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

from hsync_server.api.common import set_permissions
from hsync_server.config import HSYNC_API_KEY, HSYNC_BUCKETS
from hsync_server.file_hashes import del_md5_hash
from hsync_server.models.dir import Dir
from hsync_server.models.event import Event, EventName
from hsync_server.models.file import File
from hsync_server.routers import public_router
from fastapi import Query, WebSocket, WebSocketDisconnect
from json import loads, JSONDecodeError
from logging import getLogger
from os import makedirs, rename, remove
from os.path import exists
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


# NOTE: WebSocket does not support Header Authentication
#       and FastAPI auto-docs for WebSocket endpoints
@public_router.websocket("/events/v1/{bucket}/{cid}")
async def ws_events(
    websocket: WebSocket, bucket: str, cid: str, token: str = Query(...)
) -> None:
    """
    WebSocket endpoint for real-time file system event synchronization.

    This endpoint allows clients to:
    - Receive events from other clients in real-time
    - Send their own file system events to be broadcast
    - Trigger server-side file operations based on events

    Authentication is done via query parameter since WebSocket
    doesn't support standard HTTP headers in the same way.

    Args:
        websocket: The WebSocket connection object.
        bucket: Name of the bucket to sync events for.
        cid: Unique client ID for the connecting client.
        token: API key for authentication (passed as query parameter).

    Raises:
        ValueError: If the API key is invalid.
    """
    try:
        # Validate API key
        if token != HSYNC_API_KEY:
            message = "Invalid API KEY."
            LOG.warning(message)
            raise ValueError(message)

        # Accept the connection and register the client
        await manager.connect(websocket, cid)
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


# Global connection manager instance for WebSocket connections
manager = ConnectionManager()


LOG.info(f"Module {__name__} loaded")
