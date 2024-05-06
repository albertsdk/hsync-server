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
from loguru import logger as log
from typing import Dict, Optional

from hsync_server.models.event import Event


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
            log.debug(f"Created ConnectionManager for bucket: {bucket}")
        return self.buckets[bucket]

    def get_connection_count(self, bucket: Optional[str] = None) -> int:
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


# Global bucket connection manager instance for WebSocket connections
# Provides per-bucket connection management for better scalability
bucket_manager = BucketConnectionManager()

