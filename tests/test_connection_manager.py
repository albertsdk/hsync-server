"""
Tests for ConnectionManager and BucketConnectionManager.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from hsync_common.models.event import Event, EventName
from hsync_common.models.file import File

from hsync_server.api.connection_manager import (
    BucketConnectionManager,
    ConnectionEntry,
    ConnectionManager,
)


def _entry(ws, account_id="acct1", gen=1, bucket="bucket1"):
    """Helper to create a ConnectionEntry."""
    return ConnectionEntry(websocket=ws, account_id=account_id, key_generation=gen, bucket=bucket)


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

    def test_disconnect(self, manager):
        """Test disconnecting a client."""
        manager.active_connections["client1"] = _entry(MagicMock())
        manager.disconnect("client1")

        assert "client1" not in manager.active_connections

    def test_disconnect_nonexistent(self, manager):
        """Disconnecting non-existent client doesn't raise."""
        manager.disconnect("nonexistent")  # Should not raise

    def test_disconnect_with_websocket_only_evicts_same_socket(self, manager):
        """Cleanup of an older socket must not evict a newer registration
        that reused the same cid."""
        old_ws, new_ws = MagicMock(), MagicMock()
        manager.active_connections["client1"] = _entry(new_ws)

        manager.disconnect("client1", old_ws)  # stale cleanup: must be a no-op
        assert manager.active_connections["client1"].websocket is new_ws

        manager.disconnect("client1", new_ws)  # owner cleanup: evicts
        assert "client1" not in manager.active_connections

    @pytest.mark.asyncio
    async def test_send_message_tolerates_mutation_and_dead_sockets(self, manager):
        """A dead socket mid-broadcast is evicted without aborting delivery,
        and a concurrent disconnect during iteration must not raise."""
        from unittest.mock import MagicMock, patch

        dead = AsyncMock()
        dead.send_json.side_effect = RuntimeError("socket closed")
        live = AsyncMock()

        manager.active_connections = {
            "sender": _entry(AsyncMock()),
            "dead": _entry(dead),
            "live": _entry(live),
        }

        file = File(
            filepath="/buckets/test/test.txt", size=100, sha256sum="abc", permissions="-rw-r--r--"
        )
        event = Event(
            cid="sender", localpath="/buckets/test", bucket="test", name=EventName.added, src=file
        )

        # Mock the auth store so all connections pass the access re-check.
        mock_store = MagicMock()
        mock_acct = MagicMock()
        mock_acct.status = "active"
        mock_acct.role = "admin"
        mock_store.get_account.return_value = mock_acct
        mock_store.has_access.return_value = True

        with patch("hsync_server.api.connection_manager.get_auth_store", return_value=mock_store):
            await manager.send_message(event)

        live.send_json.assert_called_once()
        assert "dead" not in manager.active_connections

    @pytest.mark.asyncio
    async def test_send_message_broadcasts_to_others(self, manager):
        """Test that send_message broadcasts to all except sender."""
        from unittest.mock import MagicMock, patch

        ws1 = AsyncMock()
        ws2 = AsyncMock()
        ws3 = AsyncMock()

        manager.active_connections = {
            "client1": _entry(ws1),
            "client2": _entry(ws2),
            "client3": _entry(ws3),
        }

        # Create an event from client1
        file = File(
            filepath="/buckets/test/test.txt", size=100, sha256sum="abc", permissions="-rw-r--r--"
        )
        event = Event(
            cid="client1", localpath="/buckets/test", bucket="test", name=EventName.added, src=file
        )

        mock_store = MagicMock()
        mock_acct = MagicMock()
        mock_acct.status = "active"
        mock_acct.role = "admin"
        mock_store.get_account.return_value = mock_acct
        mock_store.has_access.return_value = True

        with patch("hsync_server.api.connection_manager.get_auth_store", return_value=mock_store):
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


class TestConnectionSlots:
    """Tests for the DoS-protection connection slot accounting (2.10)."""

    @pytest.fixture
    def bucket_manager(self):
        """Create a fresh BucketConnectionManager instance."""
        return BucketConnectionManager()

    def test_acquire_and_release_balance(self, bucket_manager):
        """Acquiring then releasing a slot returns counters to zero."""
        assert bucket_manager.try_acquire_slot("1.2.3.4") is True
        assert bucket_manager._total_slots == 1
        assert bucket_manager._slots_per_ip["1.2.3.4"] == 1

        bucket_manager.release_slot("1.2.3.4")
        assert bucket_manager._total_slots == 0
        assert "1.2.3.4" not in bucket_manager._slots_per_ip

    def test_per_ip_limit_enforced(self, bucket_manager):
        """A single IP cannot exceed the per-IP cap."""
        with patch(
            "hsync_server.api.connection_manager.get_ws_max_connections_per_ip",
            return_value=2,
        ):
            assert bucket_manager.try_acquire_slot("1.2.3.4") is True
            assert bucket_manager.try_acquire_slot("1.2.3.4") is True
            # Third attempt from the same IP is rejected.
            assert bucket_manager.try_acquire_slot("1.2.3.4") is False
            # A different IP is still allowed.
            assert bucket_manager.try_acquire_slot("5.6.7.8") is True

    def test_global_limit_enforced(self, bucket_manager):
        """The global cap is enforced across distinct IPs."""
        with (
            patch(
                "hsync_server.api.connection_manager.get_ws_max_connections",
                return_value=2,
            ),
            patch(
                "hsync_server.api.connection_manager.get_ws_max_connections_per_ip",
                return_value=100,
            ),
        ):
            assert bucket_manager.try_acquire_slot("1.1.1.1") is True
            assert bucket_manager.try_acquire_slot("2.2.2.2") is True
            # Global cap reached even though per-IP cap is high.
            assert bucket_manager.try_acquire_slot("3.3.3.3") is False

    def test_release_unknown_ip_is_safe(self, bucket_manager):
        """Releasing an IP that was never acquired does not go negative."""
        bucket_manager.release_slot("9.9.9.9")
        assert bucket_manager._total_slots == 0
        assert "9.9.9.9" not in bucket_manager._slots_per_ip

    def test_slot_freed_allows_reacquire(self, bucket_manager):
        """Releasing a slot frees capacity for a new connection."""
        with patch(
            "hsync_server.api.connection_manager.get_ws_max_connections_per_ip",
            return_value=1,
        ):
            assert bucket_manager.try_acquire_slot("1.2.3.4") is True
            assert bucket_manager.try_acquire_slot("1.2.3.4") is False
            bucket_manager.release_slot("1.2.3.4")
            assert bucket_manager.try_acquire_slot("1.2.3.4") is True
