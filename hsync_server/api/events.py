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

import hmac
import logging

from fastapi import WebSocket, WebSocketDisconnect
from json import loads, JSONDecodeError

from hsync_server.api.connection_manager import bucket_manager
from hsync_server.api.event_handlers import (
    handle_added_event,
    handle_deleted_event,
    handle_modified_event,
    handle_renamed_event,
    handle_symlink_event,
)
from hsync_server.config import HSYNC_API_KEY, HSYNC_BUCKETS
from hsync_server.constants import HSYNC_VERSION
from hsync_server.middleware import check_version_compatibility
from hsync_server.models.event import EventName
from hsync_server.routers import public_router


# Logger instance for this module (uses __name__ for module-level filtering)
LOG = logging.getLogger(__name__)


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
                LOG.warning(f"[WEBSOCKET ] Invalid auth message type from {cid}")
                await websocket.close(code=4001, reason="Invalid auth message")
                return

            auth_token = auth_message.get("token", "")
            # Use constant-time comparison to prevent timing attacks
            if not hmac.compare_digest(auth_token, HSYNC_API_KEY):
                LOG.warning(f"[WEBSOCKET ] Invalid API key from {cid}")
                await websocket.close(code=4003, reason="Invalid API key")
                return

            # Check client version compatibility if provided
            client_version = auth_message.get("version", "")
            if client_version:
                is_compatible, reason = check_version_compatibility(
                    HSYNC_VERSION, client_version
                )
                if not is_compatible:
                    LOG.warning(
                        f"[WEBSOCKET ] Rejecting client {cid} with incompatible "
                        f"version {client_version} (server: {HSYNC_VERSION})"
                    )
                    await websocket.send_json({
                        "type": "auth",
                        "status": "error",
                        "error": "version_incompatible",
                        "client_version": client_version,
                        "server_version": HSYNC_VERSION,
                    })
                    await websocket.close(code=4002, reason="Version incompatible")
                    return

                if reason == "minor_mismatch":
                    LOG.warning(
                        f"[WEBSOCKET ] Client {cid} version {client_version} "
                        f"differs from server {HSYNC_VERSION}"
                    )

            LOG.debug(f"[WEBSOCKET ] Client {cid} authenticated")
            # Send auth confirmation with server version
            await websocket.send_json({
                "type": "auth",
                "status": "ok",
                "version": HSYNC_VERSION,
            })

        except Exception as e:
            LOG.warning(f"[WEBSOCKET ] Auth failed for {cid}: {e}")
            await websocket.close(code=4003, reason="Authentication failed")
            return

        # Register the authenticated client with bucket-specific manager
        manager = bucket_manager.get_manager(bucket)
        manager.active_connections[cid] = websocket
        LOG.info(f"[WEBSOCKET ] Client connected: {cid} (bucket: {bucket})")
        try:
            # Main message processing loop
            while True:
                data = await websocket.receive_text()
                LOG.debug(f"[WEBSOCKET ] Received message: {data}")
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

                    # Dispatch to appropriate event handler
                    if event_name == EventName.added:
                        event = await handle_added_event(src, bucket_dir, bucket, cid)
                    elif event_name == EventName.modified:
                        event = await handle_modified_event(src, bucket_dir, bucket, cid)
                    elif event_name == EventName.deleted:
                        event = await handle_deleted_event(src, bucket_dir, bucket, cid)
                    elif event_name == EventName.renamed:
                        event = await handle_renamed_event(src, dst, bucket_dir, bucket, cid)
                    elif event_name == EventName.symlink:
                        event = await handle_symlink_event(src, bucket_dir, bucket, cid)

                    # Broadcast the event to all other connected clients
                    if event:
                        await manager.send_message(event)

                except JSONDecodeError:
                    LOG.error(f"[WEBSOCKET ] Invalid JSON received: {data}")
                except Exception as e:
                    LOG.error(f"[WEBSOCKET ] Exception data: {data}")
                    LOG.error(f"[WEBSOCKET ] Exception: {e}", exc_info=True)

        except WebSocketDisconnect:
            # Client disconnected normally
            manager.disconnect(cid)
            LOG.warning(f"[WEBSOCKET ] Disconnected (CID: {cid})")
        except Exception as e:
            # Client disconnected with error
            manager.disconnect(cid)
            LOG.warning(f"[WEBSOCKET ] Disconnected (CID: {cid}) ({e})")
            await websocket.close()
    except Exception:
        # Handle any outer exception (e.g., auth failure)
        LOG.warning(f"[WEBSOCKET ] Disconnected (CID: {cid})")
        await websocket.close()


LOG.info(f"Module {__name__} loaded")
