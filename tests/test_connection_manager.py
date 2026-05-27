"""
Tests for ConnectionManager and BucketConnectionManager.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from hsync_server.api.connection_manager import (
    BucketConnectionManager,
    ConnectionManager,
)
from hsync_server.models.event import Event, EventName
from hsync_server.models.file import File


class TestConnectionManager:
    """Tests for ConnectionManager class."""

    @pytest.fixture
    def manager(self):
        """Create a ConnectionManager instance."""
        return ConnectionManager()

    @pytest.fixture
    def mock_websocket(self):
        """Create a mock WebSocket."""
        ws = AsyncMock()
        ws.accept = AsyncMock()
        ws.send_json = AsyncMock()
        return ws

    @pytest.mark.asyncio
    async def test_connect(self, manager, mock_websocket):
        """Test connecting a client."""
        await manager.connect(mock_websocket, "client1")

        assert "client1" in manager.active_connections
        assert manager.active_connections["client1"] == mock_websocket
        mock_websocket.accept.assert_called_once()

    @pytest.mark.asyncio
    async def test_connect_multiple_clients(self, manager, mock_websocket):
        """Test connecting multiple clients."""
        ws2 = AsyncMock()
        ws2.accept = AsyncMock()

        await manager.connect(mock_websocket, "client1")
        await manager.connect(ws2, "client2")

        assert len(manager.active_connections) == 2
        assert "client1" in manager.active_connections
        assert "client2" in manager.active_connections

    def test_disconnect(self, manager):
        """Test disconnecting a client."""
        manager.active_connections["client1"] = MagicMock()
        manager.disconnect("client1")

        assert "client1" not in manager.active_connections

    def test_disconnect_nonexistent(self, manager):
        """Disconnecting non-existent client doesn't raise."""
        manager.disconnect("nonexistent")  # Should not raise

    @pytest.mark.asyncio
    async def test_send_message_broadcasts_to_others(self, manager):
        """Test that send_message broadcasts to all except sender."""
        ws1 = AsyncMock()
        ws2 = AsyncMock()
        ws3 = AsyncMock()

        manager.active_connections = {
            "client1": ws1,
            "client2": ws2,
            "client3": ws3,
        }

        # Create an event from client1
        file = File(
            filepath="/buckets/test/test.txt", size=100, sha256sum="abc", permissions="-rw-r--r--"
        )
        event = Event(
            cid="client1", localpath="/buckets/test", bucket="test", name=EventName.added, src=file
        )

        await manager.send_message(event)

        # client1 should NOT receive the message
        ws1.send_json.assert_not_called()
        # client2 and client3 should receive the message
        ws2.send_json.assert_called_once()
        ws3.send_json.assert_called_once()


class TestBucketConnectionManager:
    """Tests for BucketConnectionManager class."""

    @pytest.fixture
    def bucket_manager(self):
        """Create a BucketConnectionManager instance."""
        return BucketConnectionManager()

    def test_get_manager_creates_new(self, bucket_manager):
        """get_manager creates a new ConnectionManager for new bucket."""
        manager = bucket_manager.get_manager("bucket1")

        assert isinstance(manager, ConnectionManager)
        assert "bucket1" in bucket_manager.buckets

    def test_get_manager_returns_existing(self, bucket_manager):
        """get_manager returns existing ConnectionManager."""
        manager1 = bucket_manager.get_manager("bucket1")
        manager2 = bucket_manager.get_manager("bucket1")

        assert manager1 is manager2

    def test_get_manager_separate_buckets(self, bucket_manager):
        """Different buckets get different managers."""
        manager1 = bucket_manager.get_manager("bucket1")
        manager2 = bucket_manager.get_manager("bucket2")

        assert manager1 is not manager2

    def test_get_connection_count_empty(self, bucket_manager):
        """Connection count is 0 for empty manager."""
        assert bucket_manager.get_connection_count() == 0
        assert bucket_manager.get_connection_count("nonexistent") == 0

    def test_get_connection_count_specific_bucket(self, bucket_manager):
        """Get connection count for specific bucket."""
        manager = bucket_manager.get_manager("bucket1")
        manager.active_connections = {"c1": MagicMock(), "c2": MagicMock()}

        assert bucket_manager.get_connection_count("bucket1") == 2
        assert bucket_manager.get_connection_count("bucket2") == 0

    def test_get_connection_count_total(self, bucket_manager):
        """Get total connection count across all buckets."""
        m1 = bucket_manager.get_manager("bucket1")
        m2 = bucket_manager.get_manager("bucket2")

        m1.active_connections = {"c1": MagicMock(), "c2": MagicMock()}
        m2.active_connections = {"c3": MagicMock()}

        assert bucket_manager.get_connection_count() == 3
