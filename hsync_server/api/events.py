# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""
WebSocket events API for HSYNC Server.

Clients connect to receive/broadcast file system events.  Authentication uses
the shared account token (same as REST).  After auth, the connection is bound
to ``(session CID, account id, key_generation, bucket)``.

Re-checks (no inter-process bus):
- Every inbound frame: status, generation, expiry, membership.
- Every heartbeat ping: same re-check.
- Before every broadcast send: same re-check.

Close codes:
- 4000: invalid CID, invalid bucket name, CORS, replaced connection.
- 4001: first frame not ``auth``.
- 4002: protocol mismatch.
- 4003: auth failure (invalid key, disabled, expired, revoked).
- 4004: heartbeat timeout.
- 4008: auth timeout.
- 4009: bucket not found / no access (indistinguishable).
- 1013: connection limit.
"""

import asyncio
from collections.abc import Awaitable, Callable
from json import JSONDecodeError, loads
from pathlib import Path

from fastapi import WebSocket, WebSocketDisconnect
from hsync_common.models.event import Event, EventName, is_valid_cid
from hsync_common.path_validation import validate_remote_relpath, wire_relpaths
from hsync_common.protocol.handshake import (
    PROTOCOL_VERSION_WS,
    Capability,
    build_ws_auth_error,
    build_ws_auth_ok,
    build_ws_presence,
    decode_capabilities,
)
from hsync_common.protocol.rest import REST_BUCKET_EVENTS_PATH_TEMPLATE
from hsync_common.text import escape_loguru_markup
from loguru import logger as log

from hsync_server.api.connection_manager import (
    BucketConnectionManager,
    ConnectionManager,
    bucket_manager,
    fire_and_forget,
)
from hsync_server.api.events_handlers.handlers import (
    handle_added_event,
    handle_deleted_event,
    handle_modified_event,
    handle_renamed_event,
    handle_symlink_event,
)
from hsync_server.api.utils import is_valid_bucket
from hsync_server.auth import AuthContext, account_is_live, verify_token
from hsync_server.auth_store import get_auth_store
from hsync_server.config import (
    get_buckets_path,
    get_compression_enabled,
    get_cors_enabled,
    get_cors_origins,
    get_delta_enabled,
    get_ws_auth_timeout,
    get_ws_heartbeat_interval,
    get_ws_heartbeat_timeout,
)
from hsync_server.constants import HSYNC_VERSION, WS_MSG_TYPE_AUTH, ActionLabel
from hsync_server.rate_limiter import (
    TokenBucket,
    create_ws_message_bucket,
    resolve_client_ip,
)
from hsync_server.routers import public_router

_VALID_EVENT_NAMES = frozenset(e.value for e in EventName)

# Event handlers share one dispatch shape except for ``renamed`` (which also
# takes the destination); the ellipsis callable keeps the table heterogeneous
# without per-call ``type: ignore`` comments.
_EVENT_HANDLERS: dict[EventName, Callable[..., Awaitable[Event | None]]] = {
    EventName.added: handle_added_event,
    EventName.modified: handle_modified_event,
    EventName.deleted: handle_deleted_event,
    EventName.renamed: handle_renamed_event,
    EventName.symlink: handle_symlink_event,
}


def _event_paths_safe(payload: object) -> bool:
    """Return True when a WS event payload carries safe bucket-relative paths."""
    if payload is None:
        return True
    if not isinstance(payload, dict):
        return False
    rels = wire_relpaths(payload)
    if not rels:
        return False
    return all(validate_remote_relpath(rel) is not None for rel in rels)


def _log_disconnect(cid: str, *, error: str | None = None) -> None:
    """Log a WebSocket disconnection."""
    msg = f"{ActionLabel.WS} Disconnected (CID: {cid})"
    if error:
        msg += f" ({error})"
    log.warning(msg)


def _bucket_exists(bucket: str) -> bool:
    """Check if the bucket directory exists on disk."""
    return (Path(get_buckets_path()) / bucket).exists()


def _check_ws_access(ctx: AuthContext, bucket: str) -> bool:
    """Re-check that *ctx* still has access to *bucket*.

    Returns True if access is valid, False otherwise.  Called on every
    inbound frame, every ping, and before every broadcast.
    """
    if not _bucket_exists(bucket):
        return False
    store = get_auth_store()
    account = store.get_account(ctx.id)
    if account is None:
        return False
    if not account_is_live(account, ctx.key_generation):
        return False
    return store.has_access(ctx.id, bucket, is_admin=(ctx.role == "admin"))


async def _dispatching_authenticate(
    websocket: WebSocket,
    cid: str,
    client_ip: str,
) -> AuthContext | None:
    """Authenticate a WebSocket client via the shared account token.

    Returns the :class:`AuthContext` on success, or ``None`` on failure
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
        store = get_auth_store()
        try:
            # scrypt verification is CPU-heavy (~sub-second): run it off the
            # event loop like the REST dependency (sync def -> threadpool) so
            # other connections keep being served during a handshake.
            ctx = await asyncio.to_thread(verify_token, auth_token, store)
        except Exception:
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
            f"{ActionLabel.WS} Client {cid} authenticated (account={ctx.name}, "
            f"capabilities: {sorted(c.value for c in client_caps) or 'none'})"
        )

        server_caps: set[Capability] = {Capability.tombstones, Capability.vector_clocks}
        if get_compression_enabled():
            server_caps.add(Capability.compression)
        if get_delta_enabled():
            server_caps.add(Capability.delta)
        await websocket.send_json(
            build_ws_auth_ok(app_version=HSYNC_VERSION, capabilities=server_caps)
        )

        # Throttled last_used_at update.
        store.touch_last_used(ctx.id)

        return ctx

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
    ctx: AuthContext,
) -> None:
    """Register *websocket* under *cid* with auth metadata, evicting stale first."""
    from hsync_server.api.connection_manager import ConnectionEntry

    stale = manager.active_connections.get(cid)
    if stale is not None:
        log.warning(f"{ActionLabel.WS} cid {cid} reconnected: evicting stale registration")
        try:
            fire_and_forget(
                stale.websocket.close(code=4000, reason="Replaced by a newer connection")
            )
        except Exception:
            pass
        manager._drop_if_current(cid, stale.websocket)
    manager.active_connections[cid] = ConnectionEntry(
        websocket=websocket,
        account_id=ctx.id,
        key_generation=ctx.key_generation,
        bucket=bucket,
    )
    log.info(f"{ActionLabel.WS} Client connected: {cid} (bucket: {bucket}, account: {ctx.name})")


async def _broadcast_presence(
    bucket: str, manager: ConnectionManager, registry: BucketConnectionManager
) -> None:
    """Broadcast the bucket's presence counters to every connected client.

    Called right after a client registers and again after it disconnects (in
    ws_events), so join/leave events refresh the counter on all peers. Admins
    are operators, not share members, so both counters exclude them. The frame
    is diagnostic information, delivered through the same authz re-checks as
    file events. *registry* is the manager the connection was registered with.
    """
    connected, total = registry.presence_counts(bucket)
    await manager.send_presence(dict(build_ws_presence(connected=connected, total=total)))


async def _process_one_message(
    data: str,
    ws_bucket: TokenBucket,
    cid: str,
    bucket: str,
    manager: ConnectionManager,
    ctx: AuthContext,
) -> bool:
    """Parse and dispatch a single WebSocket text frame.

    Returns True if the connection should continue, False if it should be
    closed (authz re-check failed).
    """
    # Re-check authz before processing (after rate-limit check).
    log.debug(f"{ActionLabel.WS} Received message: {escape_loguru_markup(data)}")
    try:
        message = loads(data)

        msg_type = message.get("type")
        if msg_type == "pong":
            log.debug(f"{ActionLabel.WS} Received pong from {cid}")
            return True

        if not ws_bucket.acquire():
            log.warning(f"{ActionLabel.WS} Rate limit exceeded for {cid}, dropping event")
            return True

        # Authz re-check after rate-limit gate (avoids unbounded DB contention).
        if not _check_ws_access(ctx, bucket):
            log.warning(f"{ActionLabel.WS} Access revoked for {cid} on bucket {bucket}")
            return False

        if message.get("name") not in _VALID_EVENT_NAMES:
            log.error(f"Invalid event name: {escape_loguru_markup(str(message.get('name')))}")
            return True

        src = message.get("src", None)
        dst = message.get("dst", None)
        bucket_dir = f"{get_buckets_path()}/{bucket}"
        event_name = EventName(message.get("name"))

        if not _event_paths_safe(src) or not _event_paths_safe(dst):
            log.warning(
                f"{ActionLabel.WS} Rejected unsafe {event_name.value} event path from {cid}"
            )
            return True

        handler = _EVENT_HANDLERS.get(event_name)
        if handler is None:
            return True

        if event_name == EventName.renamed:
            event = await handler(src, dst, bucket_dir, bucket, cid)
        else:
            event = await handler(src, bucket_dir, bucket, cid)

        if event:
            # Broadcast with internal authz re-check per connection.
            await manager.send_message(event)

    except JSONDecodeError:
        log.error(f"{ActionLabel.WS} Invalid JSON received: {escape_loguru_markup(data)}")
    except Exception as e:
        log.error(f"{ActionLabel.WS} Exception data: {escape_loguru_markup(data)}")
        log.error(f"{ActionLabel.WS} Exception: {e}", exc_info=True)

    return True


async def _send_heartbeat(
    websocket: WebSocket,
    interval: float,
    cancel_event: asyncio.Event,
    cid: str,
    bucket: str,
    ctx: AuthContext,
) -> bool:
    """Send periodic ping messages, re-checking authz before each.

    Returns False if the connection should be closed (authz revoked).
    """
    while not cancel_event.is_set():
        try:
            await asyncio.sleep(interval)
            if cancel_event.is_set():
                break
            if not _check_ws_access(ctx, bucket):
                log.warning(
                    f"{ActionLabel.WS} Access revoked for {cid} on bucket {bucket} (heartbeat)"
                )
                return False
            await websocket.send_json({"type": "ping"})
        except Exception:
            break
    return True


async def _message_loop(
    websocket: WebSocket,
    cid: str,
    bucket: str,
    manager: ConnectionManager,
    ws_bucket: TokenBucket,
    heartbeat_interval: float,
    heartbeat_timeout: float,
    heartbeat_cancel: asyncio.Event,
    heartbeat_task: asyncio.Task[bool],
    ctx: AuthContext,
) -> None:
    """Main WebSocket message-processing loop with heartbeat supervision.

    If the heartbeat task returns ``False`` (access revoked), the connection
    is closed with 4009 instead of waiting for receive timeout.
    """
    try:
        while True:
            receive_task = asyncio.create_task(
                asyncio.wait_for(
                    websocket.receive_text(),
                    timeout=heartbeat_interval + heartbeat_timeout,
                )
            )
            done, _ = await asyncio.wait(
                {receive_task, heartbeat_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if heartbeat_task in done and heartbeat_task.result() is False:
                receive_task.cancel()
                log.warning(f"{ActionLabel.WS} Access revoked for {cid} (heartbeat check)")
                await websocket.close(code=4009, reason="Bucket not found")
                break
            if receive_task in done:
                try:
                    data = receive_task.result()
                except TimeoutError:
                    log.warning(f"{ActionLabel.WS} Heartbeat timeout for {cid}")
                    await websocket.close(code=4004, reason="Heartbeat timeout")
                    break
                except WebSocketDisconnect:
                    _log_disconnect(cid)
                    break

                should_continue = await _process_one_message(
                    data, ws_bucket, cid, bucket, manager, ctx
                )
                if not should_continue:
                    await websocket.close(code=4009, reason="Bucket not found")
                    break
            else:
                # Heartbeat fired but returned True (normal ping); continue.
                pass

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


@public_router.websocket(REST_BUCKET_EVENTS_PATH_TEMPLATE)
async def ws_events(websocket: WebSocket, bucket: str, cid: str) -> None:
    """WebSocket endpoint for real-time file system event synchronization."""
    slot_acquired = False
    client_ip = "unknown"
    registered: ConnectionManager | None = None
    try:
        await websocket.accept()

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

        client_ip = resolve_client_ip(
            websocket.client.host if websocket.client else None,
            websocket.headers.get("x-forwarded-for"),
        )
        if not bucket_manager.try_acquire_slot(client_ip):
            log.warning(
                f"{ActionLabel.WS} Connection limit reached, rejecting {cid} from {client_ip}"
            )
            await websocket.close(code=1013, reason="Connection limit reached")
            return
        slot_acquired = True

        # Authenticate via the shared account token.
        ctx = await _dispatching_authenticate(websocket, cid, client_ip)
        if ctx is None:
            return

        # Check bucket access (existence + membership) after auth.
        if not _check_ws_access(ctx, bucket):
            log.warning(f"{ActionLabel.WS} Bucket access denied for {cid} on bucket {bucket}")
            await websocket.close(code=4009, reason="Bucket not found")
            return

        manager = bucket_manager.get_manager(bucket)
        _register_client(manager, bucket, cid, websocket, ctx)
        registered = manager
        await _broadcast_presence(bucket, manager, bucket_manager)

        # Per-connection rate limiter and heartbeat setup.
        ws_bucket = create_ws_message_bucket()
        heartbeat_interval = get_ws_heartbeat_interval()
        heartbeat_timeout = get_ws_heartbeat_timeout()
        heartbeat_cancel = asyncio.Event()
        heartbeat_task = asyncio.create_task(
            _send_heartbeat(websocket, heartbeat_interval, heartbeat_cancel, cid, bucket, ctx)
        )

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
            ctx,
        )

    except Exception:
        _log_disconnect(cid)
        await websocket.close()
    finally:
        # The message loop removed the registration on exit (disconnect,
        # heartbeat timeout or revocation): refresh the remaining presence.
        # Doing it here — instead of after the loop — also covers the paths
        # that never reach that line (exceptions raised by the loop, asyncio
        # cancellation during shutdown), so peers never keep a stale counter.
        if registered is not None:
            try:
                await _broadcast_presence(bucket, registered, bucket_manager)
            except Exception:
                log.warning(f"{ActionLabel.WS} Presence refresh failed for {cid}")
        if slot_acquired:
            bucket_manager.release_slot(client_ip)


log.info(f"Module {__name__} loaded")
