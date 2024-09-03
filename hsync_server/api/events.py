# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

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
- Heartbeat/ping-pong for connection health monitoring
"""

import asyncio
import hmac
from json import JSONDecodeError, loads

from fastapi import WebSocket, WebSocketDisconnect
from hsync_common.text import escape_loguru_braces
from loguru import logger as log

from hsync_server.api.connection_manager import bucket_manager
from hsync_server.api.event_handlers import (
    handle_added_event,
    handle_deleted_event,
    handle_modified_event,
    handle_renamed_event,
    handle_symlink_event,
)
from hsync_server.config import (
    get_api_key,
    get_buckets_path,
    get_compression_enabled,
    get_ws_heartbeat_interval,
    get_ws_heartbeat_timeout,
)
from hsync_server.constants import HSYNC_VERSION
from hsync_server.middleware import check_version_compatibility
from hsync_server.models.event import EventName
from hsync_server.rate_limiter import create_ws_message_bucket
from hsync_server.routers import public_router


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
                log.warning(f"[WEBSOCKET ] Invalid auth message type from {cid}")
                await websocket.close(code=4001, reason="Invalid auth message")
                return

            auth_token = auth_message.get("token", "")
            # Use constant-time comparison to prevent timing attacks
            if not hmac.compare_digest(auth_token, get_api_key()):
                log.warning(f"[WEBSOCKET ] Invalid API key from {cid}")
                await websocket.close(code=4003, reason="Invalid API key")
                return

            # Check client version compatibility if provided
            client_version = auth_message.get("version", "")
            if client_version:
                is_compatible, reason = check_version_compatibility(HSYNC_VERSION, client_version)
                if not is_compatible:
                    log.warning(
                        f"[WEBSOCKET ] Rejecting client {cid} with incompatible "
                        f"version {client_version} (server: {HSYNC_VERSION})"
                    )
                    await websocket.send_json(
                        {
                            "type": "auth",
                            "status": "error",
                            "error": "version_incompatible",
                            "client_version": client_version,
                            "server_version": HSYNC_VERSION,
                        }
                    )
                    await websocket.close(code=4002, reason="Version incompatible")
                    return

                if reason == "minor_mismatch":
                    log.warning(
                        f"[WEBSOCKET ] Client {cid} version {client_version} "
                        f"differs from server {HSYNC_VERSION}"
                    )

            log.debug(f"[WEBSOCKET ] Client {cid} authenticated")
            # Send auth confirmation with server version and capabilities
            await websocket.send_json(
                {
                    "type": "auth",
                    "status": "ok",
                    "version": HSYNC_VERSION,
                    "compression_enabled": get_compression_enabled(),
                }
            )

        except Exception as e:
            log.warning(f"[WEBSOCKET ] Auth failed for {cid}: {e}")
            await websocket.close(code=4003, reason="Authentication failed")
            return

        # Register the authenticated client with bucket-specific manager
        manager = bucket_manager.get_manager(bucket)
        manager.active_connections[cid] = websocket
        log.info(f"[WEBSOCKET ] Client connected: {cid} (bucket: {bucket})")

        # Create per-connection rate limiter for event messages
        ws_bucket = create_ws_message_bucket()

        # Get heartbeat configuration
        heartbeat_interval = get_ws_heartbeat_interval()
        heartbeat_timeout = get_ws_heartbeat_timeout()

        # Create a cancel event for heartbeat task
        heartbeat_cancel = asyncio.Event()

        async def send_heartbeat() -> None:
            """Send periodic ping messages to the client."""
            while not heartbeat_cancel.is_set():
                try:
                    await asyncio.sleep(heartbeat_interval)
                    if heartbeat_cancel.is_set():
                        break
                    await websocket.send_json({"type": "ping"})
                except Exception:
                    break

        # Start heartbeat task
        heartbeat_task = asyncio.create_task(send_heartbeat())

        try:
            # Main message processing loop
            while True:
                try:
                    # Use wait_for to implement timeout
                    data = await asyncio.wait_for(
                        websocket.receive_text(), timeout=heartbeat_interval + heartbeat_timeout
                    )
                except TimeoutError:
                    log.warning(f"[WEBSOCKET ] Heartbeat timeout for {cid}")
                    await websocket.close(code=4004, reason="Heartbeat timeout")
                    break

                log.debug(f"[WEBSOCKET ] Received message: {escape_loguru_braces(data)}")
                try:
                    message = loads(data)

                    # Handle ping/pong messages
                    msg_type = message.get("type")
                    if msg_type == "pong":
                        # Client responded to ping, connection is alive
                        log.debug(f"[WEBSOCKET ] Received pong from {cid}")
                        continue

                    # Rate limit event messages (pong excluded to avoid heartbeat deadlock)
                    if not ws_bucket.acquire():
                        log.warning(f"[WEBSOCKET ] Rate limit exceeded for {cid}, dropping event")
                        continue

                    # Validate event type
                    if message.get("name") not in {
                        EventName.added.value,
                        EventName.modified.value,
                        EventName.renamed.value,
                        EventName.deleted.value,
                        EventName.symlink.value,
                    }:
                        log.error(f"Invalid event name: {message.get('name')}")
                        continue

                    # Extract event data
                    src = message.get("src", None)
                    dst = message.get("dst", None)
                    bucket_dir = f"{get_buckets_path()}/{bucket}"
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
                    log.error(f"[WEBSOCKET ] Invalid JSON received: {escape_loguru_braces(data)}")
                except Exception as e:
                    log.error(f"[WEBSOCKET ] Exception data: {escape_loguru_braces(data)}")
                    log.error(f"[WEBSOCKET ] Exception: {e}", exc_info=True)

        except WebSocketDisconnect:
            # Client disconnected normally
            log.warning(f"[WEBSOCKET ] Disconnected (CID: {cid})")
        except Exception as e:
            # Client disconnected with error
            log.warning(f"[WEBSOCKET ] Disconnected (CID: {cid}) ({e})")
            await websocket.close()
        finally:
            # Cancel heartbeat task and cleanup
            heartbeat_cancel.set()
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass
            manager.disconnect(cid)
    except Exception:
        # Handle any outer exception (e.g., auth failure)
        log.warning(f"[WEBSOCKET ] Disconnected (CID: {cid})")
        await websocket.close()


log.info(f"Module {__name__} loaded")
