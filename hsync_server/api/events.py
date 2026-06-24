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
from hsync_common.models.event import EventName
from hsync_common.protocol.handshake import (
    PROTOCOL_VERSION_WS,
    Capability,
    build_ws_auth_error,
    build_ws_auth_ok,
    decode_capabilities,
)
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
    get_ws_auth_timeout,
    get_ws_heartbeat_interval,
    get_ws_heartbeat_timeout,
)
from hsync_server.constants import HSYNC_VERSION
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

    Authentication is done via the first message after connection. The frame
    follows the shared handshake contract (``hsync_common.protocol.handshake``):
    Client sends: {"type": "auth", "token", "app_version", "protocol_version",
    "capabilities"}; the server hard-fails on a ``protocol_version`` mismatch and
    otherwise replies with {"type": "auth", "status": "ok", "protocol_version",
    "app_version", "capabilities"}.

    Args:
        websocket: The WebSocket connection object.
        bucket: Name of the bucket to sync events for.
        cid: Unique client ID for the connecting client.

    Raises:
        ValueError: If the API key is invalid.
    """
    slot_acquired = False
    client_ip = "unknown"
    try:
        # Accept the connection first (required before receiving messages)
        await websocket.accept()

        # Enforce connection caps right after accept, before any auth work, so
        # unauthenticated floods cannot exhaust file descriptors / memory.
        client_ip = websocket.client.host if websocket.client else "unknown"
        if not bucket_manager.try_acquire_slot(client_ip):
            log.warning(f"[WEBSOCKET ] Connection limit reached, rejecting {cid} from {client_ip}")
            await websocket.close(code=1013, reason="Connection limit reached")
            return
        slot_acquired = True

        # Wait for authentication message, bounded by a timeout so a client that
        # never sends the auth frame cannot hold the connection open indefinitely.
        try:
            auth_data = await asyncio.wait_for(
                websocket.receive_text(), timeout=get_ws_auth_timeout()
            )
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

            # Enforce the wire-protocol version: a mismatch is a hard failure
            # (capabilities are feature flags within a version, not a bridge to
            # earlier protocols, which do not exist).
            client_protocol = auth_message.get("protocol_version", "")
            if client_protocol != PROTOCOL_VERSION_WS:
                log.warning(
                    f"[WEBSOCKET ] Rejecting client {cid} with incompatible "
                    f"protocol {client_protocol or 'missing'} (server: {PROTOCOL_VERSION_WS})"
                )
                await websocket.send_json(
                    build_ws_auth_error(
                        error="protocol_incompatible",
                        client_protocol_version=client_protocol,
                    )
                )
                await websocket.close(code=4002, reason="Protocol incompatible")
                return

            # Client capabilities are advisory feature flags for this session.
            client_caps = decode_capabilities(auth_message.get("capabilities"))
            log.debug(
                f"[WEBSOCKET ] Client {cid} authenticated "
                f"(capabilities: {sorted(c.value for c in client_caps) or 'none'})"
            )

            # Advertise the capabilities the server supports for this session.
            server_caps: set[Capability] = set()
            if get_compression_enabled():
                server_caps.add(Capability.compression)
            await websocket.send_json(
                build_ws_auth_ok(app_version=HSYNC_VERSION, capabilities=server_caps)
            )

        except TimeoutError:
            log.warning(f"[WEBSOCKET ] Auth timeout for {cid} from {client_ip}")
            await websocket.close(code=4008, reason="Authentication timeout")
            return
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
    finally:
        # Always release the connection slot reserved right after accept().
        if slot_acquired:
            bucket_manager.release_slot(client_ip)


log.info(f"Module {__name__} loaded")
