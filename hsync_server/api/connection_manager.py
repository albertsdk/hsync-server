# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
WebSocket connection management for HSYNC Server.

This module provides classes for managing WebSocket connections:
- ConnectionManager: Manages connections for a single bucket
- BucketConnectionManager: Manages per-bucket ConnectionManager instances
"""

from fastapi import WebSocket
from hsync_common.models.event import Event
from loguru import logger as log

from hsync_server.config import get_ws_max_connections, get_ws_max_connections_per_ip
from hsync_server.constants import ActionLabel


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
        self.active_connections: dict[str, WebSocket] = {}

    def disconnect(self, client_id: str, websocket: WebSocket | None = None) -> None:
        """
        Remove a client's WebSocket connection.

        When *websocket* is given, the registration is dropped only if it
        still belongs to that very socket — so the cleanup of an older
        connection can never evict a newer one that reused the same cid.

        Args:
            client_id: The client ID to disconnect.
            websocket: The socket being closed, if known.
        """
        if websocket is None or self.active_connections.get(client_id) is websocket:
            self.active_connections.pop(client_id, None)

    async def send_message(self, event: Event) -> None:
        """
        Broadcast an event to all connected clients except the sender.

        Args:
            event: The Event object to broadcast.
        """
        # Iterate over a snapshot: registrations change concurrently as
        # clients connect/disconnect, and one dead socket must not abort
        # delivery to the remaining clients. A tuple view (no list copy) is
        # already an immutable snapshot in CPython: concurrent mutation
        # raises no RuntimeError, and any eviction below only affects the
        # next broadcasts.
        for cid, connection in tuple(self.active_connections.items()):
            # Don't send the event back to its originator
            if cid != event.cid:
                try:
                    await connection.send_json(event.to_dict())
                except Exception as e:
                    log.warning(f"{ActionLabel.WS} Dropping dead connection for {cid}: {e}")
                    if self.active_connections.get(cid) is connection:
                        self.active_connections.pop(cid, None)


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
        self.buckets: dict[str, ConnectionManager] = {}
        # Slot accounting for DoS protection. Counts every accepted connection
        # for its whole lifetime (including the pre-auth window), independently
        # of the post-auth ``active_connections`` registry used for broadcasting.
        self._total_slots: int = 0
        self._slots_per_ip: dict[str, int] = {}

    def try_acquire_slot(self, client_ip: str) -> bool:
        """
        Reserve a connection slot, enforcing global and per-IP caps.

        Must be called right after ``websocket.accept()``. The reservation has
        to be released via :meth:`release_slot` once the connection ends. The
        check/increment is atomic with respect to the asyncio event loop (no
        ``await`` in between), so concurrent handlers cannot over-commit.

        Args:
            client_ip: Direct peer IP of the connecting client.

        Returns:
            True if a slot was reserved, False if a limit would be exceeded.
        """
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
        """
        Release a previously reserved connection slot.

        Args:
            client_ip: Direct peer IP used when the slot was acquired.
        """
        self._total_slots = max(0, self._total_slots - 1)
        remaining = self._slots_per_ip.get(client_ip, 0) - 1
        if remaining > 0:
            self._slots_per_ip[client_ip] = remaining
        else:
            self._slots_per_ip.pop(client_ip, None)

    def get_manager(self, bucket: str) -> ConnectionManager:
        """
        Get or create a ConnectionManager for a specific bucket.

        Args:
            bucket: Name of the bucket.

        Returns:
            ConnectionManager instance for the specified bucket.
        """
        manager = self.buckets.get(bucket)
        if manager is None:
            manager = ConnectionManager()
            self.buckets[bucket] = manager
            log.debug(f"Created ConnectionManager for bucket: {bucket}")
        return manager


# Global bucket connection manager instance for WebSocket connections
# Provides per-bucket connection management for better scalability
bucket_manager = BucketConnectionManager()
