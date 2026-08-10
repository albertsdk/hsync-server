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
from hsync_common.constants import WS_MAX_MESSAGE_BYTES
from hsync_common.models.event import EventName, is_valid_cid
from hsync_common.path_validation import validate_remote_relpath, wire_relpaths
from hsync_common.protocol.handshake import (
    PROTOCOL_VERSION_WS,
    Capability,
    build_ws_auth_error,
    build_ws_auth_ok,
    decode_capabilities,
)
from hsync_common.text import escape_loguru_markup
from loguru import logger as log

from hsync_server.api.connection_manager import (
    ConnectionManager,
    bucket_manager,
)
from hsync_server.api.event_handlers import (
    handle_added_event,
    handle_deleted_event,
    handle_modified_event,
    handle_renamed_event,
    handle_symlink_event,
)
from hsync_server.api.utils import is_valid_bucket
from hsync_server.config import (
    get_api_key,
    get_buckets_path,
    get_compression_enabled,
    get_cors_enabled,
    get_cors_origins,
    get_ws_auth_timeout,
    get_ws_heartbeat_interval,
    get_ws_heartbeat_timeout,
)
from hsync_server.constants import HSYNC_VERSION, WS_MSG_TYPE_AUTH, ActionLabel
from hsync_server.rate_limiter import TokenBucket, create_ws_message_bucket
from hsync_server.routers import public_router

# NOTE: WebSocket does not support Header Authentication
#       and FastAPI auto-docs for WebSocket endpoints

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

# Pre-compute valid event names so the set is not rebuilt on every WS message.
_VALID_EVENT_NAMES = frozenset(e.value for e in EventName)

# Dispatch table: replace the 5-branch if/elif with a dictionary lookup.
_EVENT_HANDLERS = {
    EventName.added: handle_added_event,
    EventName.modified: handle_modified_event,
    EventName.deleted: handle_deleted_event,
    EventName.renamed: handle_renamed_event,
    EventName.symlink: handle_symlink_event,
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _event_paths_safe(payload: object) -> bool:
    """Return True when a WS event payload carries safe bucket-relative paths.

    ``None`` payloads (e.g. a missing ``dst`` on non-rename events) are safe;
    dict payloads must contain a ``filepath`` or ``path`` value accepted by
    :func:`hsync_common.path_validation.validate_remote_relpath`. Both keys may
    be present at once: validating only the first would let a payload smuggle
    a malicious second key past the gate, so every present key is validated.
    """
    if payload is None:
        return True
    if not isinstance(payload, dict):
        return False
    rels = wire_relpaths(payload)
    if not rels:
        return False
    return all(validate_remote_relpath(rel) is not None for rel in rels)


async def _send_heartbeat(
    websocket: WebSocket,
    interval: float,
    cancel_event: asyncio.Event,
) -> None:
    """Send periodic ping messages to the client."""
    while not cancel_event.is_set():
        try:
            await asyncio.sleep(interval)
            if cancel_event.is_set():
                break
            await websocket.send_json({"type": "ping"})
        except Exception:
            break


def _log_disconnect(cid: str, *, error: str | None = None) -> None:
    """Log a WebSocket disconnection, swallowing ValueError from a closed stream."""
    try:
        msg = f"{ActionLabel.WS} Disconnected (CID: {cid})"
        if error:
            msg += f" ({error})"
        log.warning(msg)
    except ValueError:
        pass


async def _dispatching_authenticate(
    websocket: WebSocket,
    cid: str,
    client_ip: str,
) -> set[Capability] | None:
    """Authenticate a WebSocket client via the shared handshake protocol.

    Returns the server's capability set on success, or ``None`` on failure
    (the connection is closed inside this function before returning).
    """
    try:
        auth_data = await asyncio.wait_for(websocket.receive_text(), timeout=get_ws_auth_timeout())
        auth_message = loads(auth_data)

        if auth_message.get("type") != WS_MSG_TYPE_AUTH:
            log.warning(f"{ActionLabel.WS} Invalid auth message type from {cid}")
            await websocket.close(code=4001, reason="Invalid auth message")
            return None

        auth_token = auth_message.get("token", "")
        if not hmac.compare_digest(auth_token, get_api_key()):
            log.warning(f"{ActionLabel.WS} Invalid API key from {cid}")
            await websocket.close(code=4003, reason="Invalid API key")
            return None

        client_protocol = auth_message.get("protocol_version", "")
        if client_protocol != PROTOCOL_VERSION_WS:
            log.warning(
                f"{ActionLabel.WS} Rejecting client {cid} with incompatible "
                f"protocol {client_protocol or 'missing'} (server: {PROTOCOL_VERSION_WS})"
            )
            await websocket.send_json(
                build_ws_auth_error(
                    error="protocol_incompatible",
                    client_protocol_version=client_protocol,
                )
            )
            await websocket.close(code=4002, reason="Protocol incompatible")
            return None

        client_caps = decode_capabilities(auth_message.get("capabilities"))
        log.debug(
            f"{ActionLabel.WS} Client {cid} authenticated "
            f"(capabilities: {sorted(c.value for c in client_caps) or 'none'})"
        )

        server_caps: set[Capability] = set()
        if get_compression_enabled():
            server_caps.add(Capability.compression)
        await websocket.send_json(
            build_ws_auth_ok(app_version=HSYNC_VERSION, capabilities=server_caps)
        )
        return server_caps

    except TimeoutError:
        log.warning(f"{ActionLabel.WS} Auth timeout for {cid} from {client_ip}")
        await websocket.close(code=4008, reason="Authentication timeout")
        return None
    except Exception as e:
        log.warning(f"{ActionLabel.WS} Auth failed for {cid}: {e}")
        await websocket.close(code=4003, reason="Authentication failed")
        return None


def _register_client(
    manager: ConnectionManager,
    bucket: str,
    cid: str,
    websocket: WebSocket,
) -> None:
    """Register *websocket* under *cid*, evicting a stale registration first."""
    stale = manager.active_connections.get(cid)
    if stale is not None:
        log.warning(f"{ActionLabel.WS} cid {cid} reconnected: evicting stale registration")
        try:
            asyncio.ensure_future(stale.close(code=4000, reason="Replaced by a newer connection"))
        except Exception:
            pass
        if manager.active_connections.get(cid) is stale:
            manager.active_connections.pop(cid, None)
    manager.active_connections[cid] = websocket
    log.info(f"{ActionLabel.WS} Client connected: {cid} (bucket: {bucket})")


async def _process_one_message(
    data: str,
    ws_bucket: TokenBucket,
    cid: str,
    bucket: str,
    manager: ConnectionManager,
) -> None:
    """Parse and dispatch a single WebSocket text frame."""
    log.debug(f"{ActionLabel.WS} Received message: {escape_loguru_markup(data)}")
    try:
        message = loads(data)

        msg_type = message.get("type")
        if msg_type == "pong":
            log.debug(f"{ActionLabel.WS} Received pong from {cid}")
            return

        if not ws_bucket.acquire():
            log.warning(f"{ActionLabel.WS} Rate limit exceeded for {cid}, dropping event")
            return

        if message.get("name") not in _VALID_EVENT_NAMES:
            log.error(f"Invalid event name: {message.get('name')}")
            return

        src = message.get("src", None)
        dst = message.get("dst", None)
        bucket_dir = f"{get_buckets_path()}/{bucket}"
        event_name = EventName(message.get("name"))

        if not _event_paths_safe(src) or not _event_paths_safe(dst):
            log.warning(
                f"{ActionLabel.WS} Rejected unsafe {event_name.value} event path from {cid}"
            )
            return

        handler = _EVENT_HANDLERS.get(event_name)
        if handler is None:
            return

        if event_name == EventName.renamed:
            event = await handler(src, dst, bucket_dir, bucket, cid)  # type: ignore[call-arg,operator]
        else:
            event = await handler(src, bucket_dir, bucket, cid)  # type: ignore[call-arg,operator]

        if event:
            await manager.send_message(event)

    except JSONDecodeError:
        log.error(f"{ActionLabel.WS} Invalid JSON received: {escape_loguru_markup(data)}")
    except Exception as e:
        log.error(f"{ActionLabel.WS} Exception data: {escape_loguru_markup(data)}")
        log.error(f"{ActionLabel.WS} Exception: {e}", exc_info=True)


async def _message_loop(
    websocket: WebSocket,
    cid: str,
    bucket: str,
    manager: ConnectionManager,
    ws_bucket: TokenBucket,
    heartbeat_interval: float,
    heartbeat_timeout: float,
    heartbeat_cancel: asyncio.Event,
    heartbeat_task: asyncio.Task[None],
) -> None:
    """Main WebSocket message-processing loop with heartbeat supervision."""
    try:
        while True:
            try:
                data = await asyncio.wait_for(
                    websocket.receive_text(),
                    timeout=heartbeat_interval + heartbeat_timeout,
                )
            except TimeoutError:
                log.warning(f"{ActionLabel.WS} Heartbeat timeout for {cid}")
                await websocket.close(code=4004, reason="Heartbeat timeout")
                break

            await _process_one_message(data, ws_bucket, cid, bucket, manager)

    except WebSocketDisconnect:
        _log_disconnect(cid)
    except Exception as e:
        _log_disconnect(cid, error=str(e))
        await websocket.close()
    finally:
        heartbeat_cancel.set()
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass
        manager.disconnect(cid, websocket)


# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------


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
        await websocket.accept()

        # Validate connection parameters before any auth work.
        if not is_valid_cid(cid):
            log.warning(f"{ActionLabel.WS} Rejecting connection with invalid cid: {cid!r}")
            await websocket.close(code=4000, reason="Invalid client ID")
            return

        if get_cors_enabled():
            origin = websocket.headers.get("origin", "")
            allowed = get_cors_origins()
            if origin and origin not in allowed:
                log.warning(f"{ActionLabel.WS} Rejecting cross-origin WS from {origin!r}")
                await websocket.close(code=4000, reason="Origin not allowed")
                return

        if not is_valid_bucket(bucket):
            log.warning(f"{ActionLabel.WS} Rejecting connection with invalid bucket: {bucket!r}")
            await websocket.close(code=4000, reason="Invalid bucket name")
            return

        client_ip = websocket.client.host if websocket.client else "unknown"
        if not bucket_manager.try_acquire_slot(client_ip):
            log.warning(
                f"{ActionLabel.WS} Connection limit reached, rejecting {cid} from {client_ip}"
            )
            await websocket.close(code=1013, reason="Connection limit reached")
            return
        slot_acquired = True

        # Authenticate via the shared handshake protocol.
        if await _dispatching_authenticate(websocket, cid, client_ip) is None:
            return

        # Register with per-bucket connection manager.
        manager = bucket_manager.get_manager(bucket)
        _register_client(manager, bucket, cid, websocket)

        # Per-connection rate limiter and heartbeat setup.
        ws_bucket = create_ws_message_bucket()
        heartbeat_interval = get_ws_heartbeat_interval()
        heartbeat_timeout = get_ws_heartbeat_timeout()
        heartbeat_cancel = asyncio.Event()
        heartbeat_task = asyncio.create_task(
            _send_heartbeat(websocket, heartbeat_interval, heartbeat_cancel)
        )

        # Run the message processing loop (handles its own cleanup).
        await _message_loop(
            websocket,
            cid,
            bucket,
            manager,
            ws_bucket,
            heartbeat_interval,
            heartbeat_timeout,
            heartbeat_cancel,
            heartbeat_task,
        )

    except Exception:
        _log_disconnect(cid)
        await websocket.close()
    finally:
        if slot_acquired:
            bucket_manager.release_slot(client_ip)


log.info(f"Module {__name__} loaded")
