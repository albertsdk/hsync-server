# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If received, this code can be found at the source:
# https://github.com/albertobasso/hsync

"""
WebSocket connection management for HSYNC Server.

- ``ConnectionManager``: per-bucket connections keyed by session CID.
  Each entry also tracks the account id and ``key_generation`` for
  revocation/re-check.
- ``BucketConnectionManager``: per-bucket managers + global/per-IP slot caps.
- ``bucket_manager``: process-wide singleton.

The registry is in-memory per worker.  Cross-worker revocation is handled by
re-checking the DB on every inbound frame, every ping, and before every
broadcast (see ``events.py``).

**Single-worker contract**: the server is deployed and supported as a single
worker only. Presence counters (``BucketConnectionManager.presence_counts``)
and broadcasts therefore always observe the authoritative registry — a
multi-worker deployment would under-count ``connected``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from fastapi import WebSocket
from hsync_common.models.event import Event
from loguru import logger as log

from hsync_server.auth_store import get_auth_store
from hsync_server.config import get_ws_max_connections, get_ws_max_connections_per_ip
from hsync_server.constants import ActionLabel


@dataclass
class ConnectionEntry:
    """A registered WebSocket connection with auth metadata."""

    websocket: WebSocket
    account_id: str
    key_generation: int
    bucket: str


class ConnectionManager:
    """Manages WebSocket connections for a single bucket.

    ``active_connections`` maps session CID → :class:`ConnectionEntry`.
    """

    def __init__(self) -> None:
        self.active_connections: dict[str, ConnectionEntry] = {}

    def disconnect(self, client_id: str, websocket: WebSocket | None = None) -> None:
        """Remove a client's WebSocket connection.

        When *websocket* is given, the registration is dropped only if it
        still belongs to that very socket.  Empty per-bucket managers are
        pruned to prevent unbounded memory growth.
        """
        entry = self.active_connections.get(client_id)
        if entry is None:
            return
        if websocket is None or entry.websocket is websocket:
            self.active_connections.pop(client_id, None)

    def _prune_if_empty(self) -> None:
        """Remove this manager from its parent if it has no connections."""
        pass  # Called by BucketConnectionManager after disconnect

    async def _send_to_all(self, payload: dict, skip_cid: str | None = None) -> None:
        """Send *payload* to every connected client, re-checking authz per recipient.

        ``skip_cid`` excludes one sender (file events must not be echoed back
        to the connection that produced them; broadcast frames use ``None``).
        Dead sockets and revoked accounts are dropped silently — the same
        re-checks ``send_message`` has always applied.
        """
        store = get_auth_store()
        for cid, entry in tuple(self.active_connections.items()):
            if skip_cid is not None and cid == skip_cid:
                continue
            # Re-check recipient access before delivering.
            account = store.get_account(entry.account_id)
            if account is None or account.status != "active":
                if (
                    self.active_connections.get(cid) is not None
                    and self.active_connections[cid].websocket is entry.websocket
                ):
                    self.active_connections.pop(cid, None)
                continue
            if not store.has_access(
                entry.account_id, entry.bucket, is_admin=(account.role == "admin")
            ):
                if (
                    self.active_connections.get(cid) is not None
                    and self.active_connections[cid].websocket is entry.websocket
                ):
                    self.active_connections.pop(cid, None)
                continue
            try:
                await entry.websocket.send_json(payload)
            except Exception as e:
                log.warning(f"{ActionLabel.WS} Dropping dead connection for {cid}: {e}")
                if (
                    self.active_connections.get(cid) is not None
                    and self.active_connections[cid].websocket is entry.websocket
                ):
                    self.active_connections.pop(cid, None)

    async def send_message(self, event: Event) -> None:
        """Broadcast an event to all connected clients except the sender.

        Before delivering to each connection, the recipient's authz is
        re-checked (membership/status/generation) against the auth store.
        Dead sockets and revoked accounts are dropped silently.
        """
        await self._send_to_all(event.to_dict(), skip_cid=event.cid)

    async def send_presence(self, payload: dict) -> None:
        """Broadcast a presence frame to every connected client.

        Uses the exact recipient re-checks of :meth:`send_message` but without
        the sender filter: presence is bucket-wide state, not a reply to one
        connection.
        """
        await self._send_to_all(payload)


class BucketConnectionManager:
    """Manages per-bucket ConnectionManager instances and slot accounting."""

    def __init__(self) -> None:
        self.buckets: dict[str, ConnectionManager] = {}
        self._total_slots: int = 0
        self._slots_per_ip: dict[str, int] = {}

    def try_acquire_slot(self, client_ip: str) -> bool:
        """Reserve a connection slot, enforcing global and per-IP caps."""
        if self._total_slots >= get_ws_max_connections():
            log.warning(f"WS global connection limit reached ({self._total_slots})")
            return False
        if self._slots_per_ip.get(client_ip, 0) >= get_ws_max_connections_per_ip():
            log.warning(f"WS per-IP connection limit reached for {client_ip}")
            return False
        self._total_slots += 1
        self._slots_per_ip[client_ip] = self._slots_per_ip.get(client_ip, 0) + 1
        return True

    def release_slot(self, client_ip: str) -> None:
        """Release a previously reserved connection slot."""
        self._total_slots = max(0, self._total_slots - 1)
        remaining = self._slots_per_ip.get(client_ip, 0) - 1
        if remaining > 0:
            self._slots_per_ip[client_ip] = remaining
        else:
            self._slots_per_ip.pop(client_ip, None)

    def get_manager(self, bucket: str) -> ConnectionManager:
        """Get or create a ConnectionManager for a specific bucket."""
        manager = self.buckets.get(bucket)
        if manager is None:
            manager = ConnectionManager()
            self.buckets[bucket] = manager
            log.debug(f"Created ConnectionManager for bucket: {bucket}")
        return manager

    def presence_counts(self, bucket: str) -> tuple[int, int]:
        """Compute ``(connected, total)`` presence counters for *bucket*.

        ``total`` is the number of clients (accounts, non-admin) with bucket
        membership — the share as the central registry defines it. ``connected``
        is the number of those accounts with a live WebSocket on this worker.
        Admins are operators, not share members: they are excluded from both
        counters.

        Correct only under the supported single-worker deployment (see the
        module docstring); in a multi-worker setup ``connected`` would only
        count this worker's sockets.
        """
        store = get_auth_store()
        member_ids = store.get_share_member_ids(bucket)
        manager = self.buckets.get(bucket)
        connected_ids = (
            {e.account_id for e in manager.active_connections.values()} if manager else set()
        )
        return len(connected_ids & member_ids), len(member_ids)

    def _prune_empty_managers(self) -> None:
        """Remove bucket managers with no active connections."""
        empty = [b for b, m in self.buckets.items() if not m.active_connections]
        for b in empty:
            self.buckets.pop(b, None)

    # ------------------------------------------------------------------
    # Account-level close (rotate-key immediate / disable / delete)
    # ------------------------------------------------------------------

    def close_by_account(
        self, account_id: str, *, code: int = 4003, reason: str = "Authentication revoked"
    ) -> None:
        """Close all connections for *account_id* across all buckets (local worker)."""
        for bucket, manager in list(self.buckets.items()):
            for cid, entry in list(manager.active_connections.items()):
                if entry.account_id == account_id:
                    try:
                        asyncio.ensure_future(entry.websocket.close(code=code, reason=reason))
                    except Exception:
                        pass
                    manager.active_connections.pop(cid, None)
                    log.info(
                        f"{ActionLabel.WS} Closed connection {cid} (account {account_id}, bucket {bucket})"
                    )
        self._prune_empty_managers()

    def close_by_account_bucket(
        self, account_id: str, bucket: str, *, code: int = 4009, reason: str = "Bucket not found"
    ) -> None:
        """Close connections for *account_id* on a specific *bucket* only."""
        manager = self.buckets.get(bucket)
        if manager is None:
            return
        for cid, entry in list(manager.active_connections.items()):
            if entry.account_id == account_id:
                try:
                    asyncio.ensure_future(entry.websocket.close(code=code, reason=reason))
                except Exception:
                    pass
                manager.active_connections.pop(cid, None)
                log.info(
                    f"{ActionLabel.WS} Closed connection {cid} (account {account_id}, bucket {bucket})"
                )
        self._prune_empty_managers()


# Global singleton.
bucket_manager = BucketConnectionManager()
